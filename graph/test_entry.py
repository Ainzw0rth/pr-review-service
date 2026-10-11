"""Run inside the graph image: real parser, isolated temporary cache."""

import base64
import importlib.util
import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("adapter", "/opt/graph-entry.py")
adapter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(adapter)


def snapshot(sha, files):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, content in files.items():
            data = content.encode()
            entry = tarfile.TarInfo("repo/" + name)
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return {"sha": sha * 40, "archive": base64.b64encode(buffer.getvalue()).decode()}


class CacheTests(unittest.TestCase):
    def test_warm_update_deletion_switchback_and_isolation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one = snapshot(
                "a",
                {
                    "lib.py": "def check():\n    return True\n",
                    "caller.py": "from lib import check\ndef call():\n    return check()\n",
                },
            )
            two = snapshot("b", {"caller.py": "def call():\n    return False\n"})

            def run(snap, repo="test/one"):
                return adapter.run(
                    {"repository": repo, "snapshots": [snap], "changed": ["lib.py"]}, root
                )

            cold = run(one)
            warm = run(one)
            self.assertEqual(cold["snapshots"][0]["mode"], "full")
            self.assertEqual(warm["snapshots"][0]["files_parsed"], 0)
            self.assertIn("caller.py", cold["paths"])
            removed = run(two)
            self.assertEqual(removed["head_sha"], "b" * 40)
            self.assertNotIn("lib.py", removed["paths"])
            back = run(one)
            self.assertEqual(back["snapshots"][0]["mode"], "incremental")
            self.assertEqual(back["snapshots"][0]["nodes"], cold["snapshots"][0]["nodes"])
            self.assertIn("caller.py", back["paths"])
            self.assertEqual(run(one, "test/two")["snapshots"][0]["mode"], "full")

    def test_parse_error_invalidates_cache_and_next_run_rebuilds(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory)
            one = snapshot("a", {"lib.py": "def check():\n    return True\n"})
            adapter.update(cache, one)
            with patch.object(
                adapter, "incremental_update", return_value={"errors": [{"file": "lib.py"}]}
            ):
                with self.assertRaises(RuntimeError):
                    adapter.update(cache, one)
            self.assertFalse((cache / "state.json").exists())
            self.assertEqual(adapter.update(cache, one)["mode"], "full")

    def test_base_graph_preserves_dependents_of_removed_definition(self):
        with tempfile.TemporaryDirectory() as directory:
            one = snapshot(
                "a",
                {
                    "lib.py": "def check():\n    return True\n",
                    "caller.py": "from lib import check\ndef call():\n    return check()\n",
                },
            )
            two = snapshot(
                "b",
                {
                    "lib.py": "# removed\n",
                    "caller.py": "from lib import check\ndef call():\n    return check()\n",
                },
            )
            result = adapter.run(
                {"repository": "test/one", "snapshots": [one, two], "changed": ["lib.py"]},
                Path(directory),
            )
            self.assertIn("caller.py", result["paths"])
            self.assertEqual(result["head_sha"], "b" * 40)


if __name__ == "__main__":
    unittest.main()
