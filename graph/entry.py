"""Isolated CRG adapter: one locked, content-reconciled cache per repository."""

import base64
import fcntl
import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

from code_review_graph.graph import GraphStore
from code_review_graph.incremental import full_build, incremental_update

from core.runner import extract_archive

VERSION = "crg-2.3.9-adapter-1"


def file_hashes(root):
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in root.rglob("*")
        if p.is_file()
    }


def update(cache, snapshot):
    sha = snapshot["sha"]
    if not re.fullmatch(r"[a-f0-9]{40}", sha):
        raise ValueError("Invalid snapshot SHA")
    source, staging = cache / "source", cache / "staging"
    state_path = cache / "state.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else {}
    if previous.get("version") != VERSION:
        previous = {}
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    extract_archive(base64.b64decode(snapshot["archive"], validate=True), staging)
    # Repository configuration remains data; no Git, package install or project hooks run.
    shutil.rmtree(staging / ".git", ignore_errors=True)
    hashes = file_hashes(staging)
    old = previous.get("hashes", {})
    changed = sorted(p for p in old.keys() | hashes.keys() if old.get(p) != hashes.get(p))
    # A crash/error after this point invalidates the cache. Never reuse partially updated rows.
    state_path.unlink(missing_ok=True)
    shutil.rmtree(source, ignore_errors=True)
    staging.rename(source)
    db = cache / "graph.db"
    if not previous:
        for path in cache.glob("graph.db*"):
            path.unlink()
    started = time.monotonic()
    store = GraphStore(db)
    try:
        result = (
            incremental_update(source, store, changed_files=changed)
            if previous
            else full_build(source, store)
        )
        if result.get("errors"):
            raise RuntimeError("Graph parsing incomplete")
        stats = store.get_stats()
        summary = {
            "sha": sha,
            "mode": "incremental" if previous else "full",
            "changed_files": len(changed),
            "files_parsed": result.get("files_updated", result.get("files_parsed", 0)),
            "indexed_files": stats.files_count,
            "nodes": stats.total_nodes,
            "edges": stats.total_edges,
            "seconds": round(time.monotonic() - started, 3),
        }
        state = {"version": VERSION, "sha": sha, "hashes": hashes, "summary": summary}
        temporary = cache / "state.tmp"
        temporary.write_text(json.dumps(state))
        temporary.replace(state_path)
        return summary
    finally:
        store.close()


def impact(cache, changed):
    root = cache / "source"
    store = GraphStore(cache / "graph.db")
    try:
        result = store.get_impact_radius(
            [str(root / p) for p in changed],
            max_depth=2,
            max_nodes=max(1, store.get_stats().total_nodes),
        )
        if result.get("truncated"):
            raise RuntimeError("Graph impact truncated")
        paths = set(result["impacted_files"])
        # Include proven outgoing dependencies as well as reverse-impact dependents.
        for path in changed:
            for node in store.get_nodes_by_file(str(root / path)):
                for edge in store.get_edges_by_source(node.qualified_name):
                    target = store.get_node(edge.target_qualified)
                    if target and target.file_path:
                        paths.add(target.file_path)
        return {str(Path(p).relative_to(root)) for p in paths}
    finally:
        store.close()


def run(request, root=Path("/graph-cache")):
    repo = request["repository"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        raise ValueError("Invalid repository")
    changed = request.get("changed", [])
    for path in changed:
        if (
            not isinstance(path, str)
            or Path(path).is_absolute()
            or ".." in Path(path).parts
            or "\\" in path
        ):
            raise ValueError("Invalid evidence path")
    cache = root / hashlib.sha256(repo.lower().encode()).hexdigest()
    cache.mkdir(parents=True, exist_ok=True)
    with (cache / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        summaries, paths = [], set()
        for snapshot in request["snapshots"]:
            summaries.append(update(cache, snapshot))
            paths |= impact(cache, changed)
        # Base-graph dependents remain useful for deleted/renamed definitions. Supply HEAD only.
        source = cache / "source"
        paths = sorted(p for p in paths if (source / p).is_file())
        return {
            "version": VERSION,
            "repository": repo,
            "head_sha": summaries[-1]["sha"],
            "snapshots": summaries,
            "paths": paths,
            "depth": 2,
        }


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(300 * 1024 * 1024 + 1)
        if len(raw) > 300 * 1024 * 1024:
            raise ValueError("Graph input too large")
        print(json.dumps(run(json.loads(raw))))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__}), flush=True)
        sys.exit(1)
