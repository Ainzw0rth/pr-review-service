import fcntl
import hashlib
import json
import tempfile
from pathlib import Path

from django.test import SimpleTestCase, override_settings


class GraphStatusTests(SimpleTestCase):
    def test_marker_requires_database_and_valid_version_and_lock_is_nonblocking(self):
        from core.graph_status import status

        with (
            tempfile.TemporaryDirectory() as directory,
            override_settings(GRAPH_STATUS_DIR=directory, GRAPH_REPOSITORIES=["org/repo"]),
        ):
            self.assertEqual(status("org/repo")["state"], "missing")
            cache = Path(directory) / hashlib.sha256(b"org/repo").hexdigest()
            cache.mkdir()
            (cache / "lock").touch()
            (cache / "state.json").write_text(
                json.dumps({"version": "crg-2.3.9-adapter-1", "sha": "a" * 40, "hashes": {}})
            )
            self.assertEqual(status("org/repo")["state"], "invalid")
            (cache / "graph.db").touch()
            (cache / "source").mkdir()
            self.assertEqual(status("org/repo")["sha"], "a" * 40)
            self.assertEqual(status("org/repo")["state"], "ready")
            with (cache / "lock").open("r") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                self.assertEqual(status("org/repo")["state"], "updating")
            (cache / "state.json").write_text("{bad")
            self.assertEqual(status("org/repo")["state"], "invalid")
            self.assertEqual(status("other/repo")["state"], "disabled")

    def test_unmounted_cache_never_claims_missing_or_ready(self):
        from core.graph_status import status

        with override_settings(GRAPH_STATUS_DIR="", GRAPH_REPOSITORIES=["org/repo"]):
            self.assertEqual(status("org/repo")["state"], "unavailable")
