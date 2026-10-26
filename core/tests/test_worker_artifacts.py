"""Archive trust boundary tests run without Docker or Django."""

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from execution.storage import RunnerError, extract_archive


def archive(entries):
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as tar:
        for name, kind, value in entries:
            info = tarfile.TarInfo(name)
            info.type = kind
            if kind == tarfile.REGTYPE:
                info.size = len(value)
                tar.addfile(info, io.BytesIO(value))
            else:
                info.linkname = value
                tar.addfile(info)
    return output.getvalue()


class WorkerArchiveTests(unittest.TestCase):
    def test_extract_strips_root(self):
        with tempfile.TemporaryDirectory() as directory:
            extract_archive(
                archive([("repo/code.py", tarfile.REGTYPE, b"print(1)")]), Path(directory)
            )
            self.assertEqual((Path(directory) / "code.py").read_bytes(), b"print(1)")

    def test_rejects_traversal_links_duplicate_roots_and_devices(self):
        cases = [
            [("repo/../../escape", tarfile.REGTYPE, b"x")],
            [("repo/link", tarfile.SYMTYPE, "/etc/passwd")],
            [("repo/link", tarfile.LNKTYPE, "repo/a")],
            [("repo/device", tarfile.CHRTYPE, "")],
            [("repo/a", tarfile.REGTYPE, b"x"), ("repo/a", tarfile.REGTYPE, b"y")],
            [("one/a", tarfile.REGTYPE, b"x"), ("two/b", tarfile.REGTYPE, b"y")],
        ]
        for entries in cases:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(RunnerError):
                    extract_archive(archive(entries), Path(directory))

    def test_rejects_decompression_limit(self):
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("execution.storage.MAX_TAR_BYTES", 512),
        ):
            with self.assertRaises(RunnerError):
                extract_archive(
                    archive([("repo/a", tarfile.REGTYPE, b"a" * 4096)]), Path(directory)
                )
