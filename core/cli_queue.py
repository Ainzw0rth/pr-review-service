"""Same-host priority admission; stable resource/gate inodes, ephemeral live tickets.

All inference entrants must use this queue. Legacy/recovery resource holders are
counted, but cannot participate in ordering if they bypass this gate.
"""

import fcntl
import os
import time
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import replace

from django.conf import settings

from .capacity import CapacityBusy, CapacityError, _permit, cli_limit, digest


def _live_tickets(directory, own):
    """Gate held: a ticket is live exactly while its owner holds its flock."""
    tickets = []
    for path in directory.glob("*.ticket"):
        try:
            order, priority, nonce = path.stem.split("-")
            key = (int(priority), int(order), nonce)
        except ValueError:
            raise CapacityError() from None
        if path == own:
            tickets.append((key, path))
            continue
        fd = os.open(path, os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            if os.fstat(fd).st_ino != path.stat().st_ino:
                raise CapacityError()
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                tickets.append((key, path))
            else:
                path.unlink()
        finally:
            os.close(fd)
    return sorted(tickets)


def _physical_slot(scope, limit, cancelled):
    """Snapshot every occupied index, including old slots above a lowered cap."""
    with ExitStack() as free:
        available = []
        for index in range(32):
            owner = ExitStack()
            try:
                slot = owner.enter_context(_permit(scope, [index], cancelled, wait=False))
            except CapacityBusy:
                owner.close()
                continue
            except BaseException:
                owner.close()
                raise
            free.callback(owner.close)
            available.append((slot, owner))
        if 32 - len(available) >= limit:
            return None
        slot, owner = next(item for item in available if item[0].index < limit)
        return slot, owner.pop_all()


@contextmanager
def admit_cli(scope: str, cancelled, *, on_wait=None, wait=True, priority=1):
    """Priorities 0 synthesis, 1 review, 2 quota; FIFO within each class.

    Running sessions retain their exact resource lock through context exit.
    Capacity changes affect future admission only; waiting performs no DB calls.
    """
    from .providers import ReviewCancelled

    if type(priority) is not int or priority not in (0, 1, 2):
        raise CapacityError()
    scope_hash = digest(scope)
    gate = digest("cli-queue:" + scope)
    directory = settings.DATA_DIR / "capacity" / (gate + ".queue")
    ticket = None
    ticket_fd = None
    held = None
    waiting = False
    try:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        while held is None:
            if cancelled():
                raise ReviewCancelled()
            with _permit(gate, [0], cancelled, wait=wait):
                tickets = _live_tickets(directory, ticket)
                if ticket is None:
                    # Sequence assigned under the gate; no wall-clock dependence.
                    order = max((key[1] for key, _ in tickets), default=-1) + 1
                    ticket = directory / f"{order:020}-{priority}-{uuid.uuid4().hex}.ticket"
                    ticket_fd = os.open(
                        ticket,
                        os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
                        0o600,
                    )
                    fcntl.flock(ticket_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    tickets.append(((priority, order, ticket.stem.rsplit("-", 1)[1]), ticket))
                    tickets.sort()
                if tickets[0][1] == ticket:
                    current_limit = cli_limit()
                    if type(current_limit) is not int or not 1 <= current_limit <= 32:
                        raise CapacityError()
                    held = _physical_slot(scope_hash, current_limit, cancelled)
                    if held is not None:
                        held = (replace(held[0], admission_limit=current_limit), held[1])
                        # Fail closed before exposing a resource if queue cleanup fails.
                        ticket.unlink()
                        ticket = None
            if held is not None:
                break
            if not wait:
                raise CapacityBusy()
            if not waiting:
                waiting = True
                if on_wait:
                    on_wait(True)
            time.sleep(0.05)
        if cancelled():
            raise ReviewCancelled()
        if waiting:
            waiting = False
            if on_wait:
                on_wait(False)
        yield held[0]
    except OSError:
        raise CapacityError() from None
    finally:
        try:
            try:
                if ticket is not None:
                    with _permit(gate, [0], lambda: False):
                        ticket.unlink()
            finally:
                try:
                    if ticket_fd is not None:
                        os.close(ticket_fd)
                finally:
                    try:
                        if held is not None:
                            held[1].close()
                    finally:
                        if waiting and on_wait:
                            on_wait(False)
        except OSError:
            raise CapacityError() from None
