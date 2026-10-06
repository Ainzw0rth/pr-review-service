"""Capture review evidence. Every truncation is explicit and blocks clearance."""

import base64
import binascii
import json
import posixpath
import re
from pathlib import PurePosixPath
from urllib.parse import quote

from .github import MAX_PAGES, IntegrationError

MAX_CONTEXT_BYTES = 1_500_000
MAX_SOURCE_FILES = 150
MAX_RELATED = 25
THREAD_FIELDS = """id isResolved path line comments(first:100) {
 nodes { id databaseId body author { login } } pageInfo { hasNextPage endCursor }
}"""
THREAD_QUERY = """query($owner:String!,$name:String!,$number:Int!,$cursor:String) {
 repository(owner:$owner,name:$name) { pullRequest(number:$number) {
 reviewThreads(first:100,after:$cursor) { nodes { THREAD_FIELDS } pageInfo { hasNextPage endCursor } }
 } } }""".replace("THREAD_FIELDS", THREAD_FIELDS)
COMMENT_QUERY = """query($id:ID!,$cursor:String) { node(id:$id) {
 ... on PullRequestReviewThread { comments(first:100,after:$cursor) {
 nodes { id databaseId body author { login } } pageInfo { hasNextPage endCursor }
 } } } }"""


def _next(connection, seen):
    page = connection["pageInfo"]
    if not page["hasNextPage"]:
        return None
    cursor = page.get("endCursor")
    if not cursor or cursor in seen:
        raise IntegrationError("GitHub discussion pagination is incomplete.")
    seen.add(cursor)
    return cursor


def threads(github, full_name, number):
    owner, name = full_name.split("/")
    result, seen, cursor = [], set(), None
    for _ in range(MAX_PAGES):
        value = github.graphql(
            THREAD_QUERY,
            {"owner": owner, "name": name, "number": number, "cursor": cursor},
        )
        connection = value["repository"]["pullRequest"]["reviewThreads"]
        for thread in connection["nodes"]:
            if thread["isResolved"]:
                continue
            comments = thread["comments"]
            nodes = list(comments["nodes"])
            comment_seen = set()
            comment_cursor = _next(comments, comment_seen)
            for _ in range(MAX_PAGES):
                if not comment_cursor:
                    break
                more = github.graphql(
                    COMMENT_QUERY, {"id": thread["id"], "cursor": comment_cursor}
                )["node"]["comments"]
                nodes.extend(more["nodes"])
                if len(json.dumps(nodes).encode()) > MAX_CONTEXT_BYTES:
                    raise IntegrationError("Discussion context exceeds supported size.")
                comment_cursor = _next(more, comment_seen)
            if comment_cursor:
                raise IntegrationError("GitHub discussion exceeds supported context limit.")
            result.append({**thread, "comments": nodes})
            if len(json.dumps(result).encode()) > MAX_CONTEXT_BYTES:
                raise IntegrationError("Discussion context exceeds supported size.")
        cursor = _next(connection, seen)
        if not cursor:
            return result
    raise IntegrationError("GitHub discussion exceeds supported context limit.")


def valid_lines(patch):
    """Right-side diff locations accepted for inline findings."""
    output, line = [], None
    for row in patch.splitlines():
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@", row)
        if match:
            line = int(match[1])
        elif line is not None and row.startswith(("+", " ")):
            output.append(line)
            line += 1
    return output


def _path(path):
    if (
        not isinstance(path, str)
        or not path
        or "\\" in path
        or "\x00" in path
        or path.startswith("/")
        or any(part in ("", ".", "..") for part in path.split("/"))
    ):
        raise IntegrationError("Repository contains an unsupported source path.")
    return path


def _source(github, path, sha):
    data = github.get("/contents/" + quote(_path(path), safe="/") + "?ref=" + sha)
    if not isinstance(data, dict) or data.get("type") != "file" or data.get("encoding") != "base64":
        raise IntegrationError("A required source file is unavailable or too large.")
    try:
        content = base64.b64decode(data["content"].replace("\n", ""), validate=True).decode("utf-8")
    except (KeyError, ValueError, UnicodeDecodeError, binascii.Error):
        raise IntegrationError("A required source file is not reviewable text.") from None
    return {"path": path, "content": content}


def _references(text, full_name):
    # Strip URLs/repo-qualified references before accepting unqualified #numbers.
    explicit = re.findall(
        r"https://github\.com/" + re.escape(full_name) + r"/(?:issues|pull)/(\d+)\b",
        text,
        re.IGNORECASE,
    )
    explicit += re.findall(
        r"(?<![\w/.-])" + re.escape(full_name) + r"#(\d+)\b", text, re.IGNORECASE
    )
    rest = re.sub(r"https?://\S+|[\w.-]+/[\w.-]+#\d+", "", text)
    explicit += re.findall(r"(?<![\w/#])#(\d+)\b", rest)
    return {int(number) for number in explicit if int(number) > 0}


def build_context(github, job):
    repo = job.pr.repository
    result = {
        "complete": False,
        "reason": "",
        "pr": {},
        "head_sha": job.head_sha,
        "files": [],
        "issues": [],
        "related_prs": [],
        "threads": [],
        "docs": [],
        "sources": [],
        "tasks": [],
        "instructions": repo.instructions,
    }

    def bound():
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES:
            raise IntegrationError(
                "Review context exceeds supported size; split the PR or narrow its scope."
            )

    try:
        pr = github.pull(job.pr.number)
        result["pr"] = pr
        if pr["head"]["sha"] != job.head_sha:
            raise IntegrationError("The pull request head changed; request a fresh review.")
        if not re.fullmatch(r"[a-fA-F0-9]{40,64}", job.head_sha):
            raise IntegrationError("Invalid source commit.")
        files = github.pages(f"/pulls/{job.pr.number}/files")
        if len(files) != pr.get("changed_files"):
            raise IntegrationError("The pull request file list is incomplete.")
        if len(files) > MAX_SOURCE_FILES:
            raise IntegrationError("The change exceeds supported source context size.")
        for file in files:
            filename = _path(file["filename"])
            patch = file.get("patch")
            if not patch:
                raise IntegrationError(
                    "A changed file has no reviewable diff; approval requires complete evidence."
                )
            for prefix, count_key in (("+", "additions"), ("-", "deletions")):
                if (
                    count_key in file
                    and sum(row.startswith(prefix) for row in patch.splitlines()) != file[count_key]
                ):
                    raise IntegrationError(
                        "A changed file diff is truncated; complete evidence is required."
                    )
            entry = {
                "filename": filename,
                "status": file["status"],
                "patch": patch,
                "valid_lines": valid_lines(patch),
            }
            if file["status"] != "removed":
                source = _source(github, filename, job.head_sha)
                entry["content"] = source["content"]
                result["sources"].append(source)
            result["files"].append(entry)
            bound()
        result["threads"] = threads(github, repo.full_name, job.pr.number)
        result["comments"] = github.pages(f"/issues/{job.pr.number}/comments")
        text = (pr.get("body") or "") + "\n" + pr.get("title", "")
        references = _references(text, repo.full_name) - {job.pr.number}
        config = repo.sheet_config or {}
        task_map = config.get("tasks", {}) if config.get("confirmed") else {}
        for task_id, task in task_map.items():
            if job.pr.number in task.get("prs", []):
                result["tasks"].append({"id": task_id, **task})
                references.update(task.get("prs", []))
                if task.get("issue"):
                    references.add(task["issue"])
        references.discard(job.pr.number)
        if len(references) > MAX_RELATED:
            raise IntegrationError("Too many linked issues or pull requests for complete review.")
        processed = {job.pr.number}
        while references - processed:
            if len(processed | references) > MAX_RELATED + 1:
                raise IntegrationError(
                    "Too many linked issues or pull requests for complete review."
                )
            number = min(references - processed)
            processed.add(number)
            issue = github.get(f"/issues/{number}")
            if "pull_request" in issue:
                related = github.pull(number)
                related["threads"] = threads(github, repo.full_name, number)
                related["comments"] = github.pages(f"/issues/{number}/comments")
                related["files"] = github.pages(f"/pulls/{number}/files")
                result["related_prs"].append(related)
            else:
                issue["comments"] = github.pages(f"/issues/{number}/comments")
                result["issues"].append(issue)
                linked_text = (
                    (issue.get("body") or "")
                    + "\n"
                    + "\n".join(comment.get("body") or "" for comment in issue["comments"])
                )
                references.update(_references(linked_text, repo.full_name))
            bound()
        if result["tasks"] and repo.sheet_credential:
            from .sheets import task_context

            result["tasks"] = task_context(repo, [task["id"] for task in result["tasks"]])
        tree = github.get(f"/git/trees/{job.head_sha}?recursive=1")
        if tree.get("truncated"):
            raise IntegrationError(
                "Source tree is incomplete; relevant documentation cannot be verified."
            )
        paths = {node["path"] for node in tree["tree"] if node["type"] == "blob"}
        text_context = "\n".join(
            [
                text,
                *(issue.get("body") or "" for issue in result["issues"]),
                *(
                    comment.get("body") or ""
                    for issue in result["issues"]
                    for comment in issue["comments"]
                ),
            ]
        )
        source_text = "\n".join(source["content"] for source in result["sources"])
        existing = {source["path"] for source in result["sources"]}
        doc_paths = {
            path
            for path in paths
            if path.startswith("docs/") and path.lower().endswith((".md", ".mdx", ".rst", ".txt"))
        }
        keywords = {
            token.lower()
            for file in files
            for token in re.findall(r"[A-Za-z0-9]+", posixpath.splitext(file["filename"])[0])
            if len(token) > 2
        } - {"src", "lib", "test", "tests", "index", "docs"}
        indexes = {"docs/README.md", "docs/index.md", "docs/index.mdx"} & doc_paths
        pending = indexes | {
            path
            for path in doc_paths
            if path in text_context
            or path in source_text
            or path in existing
            or keywords.intersection(re.findall(r"[a-z0-9]+", path.lower()))
        }
        visited = set()
        while pending - visited:
            if len(visited | pending) > MAX_SOURCE_FILES:
                raise IntegrationError("Relevant documentation exceeds supported context size.")
            path = min(pending - visited)
            visited.add(path)
            doc = _source(github, path, job.head_sha)
            result["docs"].append(doc)
            bound()
            # Resolve explicit Markdown links against the referring document.
            for target in re.findall(r"\]\(([^)\s]+)", doc["content"]):
                if "://" in target or target.startswith("#"):
                    continue
                target = target.split("#")[0]
                relative = posixpath.normpath(posixpath.join(posixpath.dirname(path), target))
                candidate = target if target in doc_paths else relative
                # Indexes orient review; unrelated index entries are not a full-doc audit.
                if candidate in doc_paths and (
                    path not in indexes
                    or candidate in text_context
                    or keywords.intersection(re.findall(r"[a-z0-9]+", candidate.lower()))
                ):
                    pending.add(candidate)
            for candidate in doc_paths:
                if candidate in doc["content"] and path not in indexes:
                    pending.add(candidate)
        existing |= visited
        references_text = (
            text_context
            + "\n"
            + source_text
            + "\n"
            + "\n".join(doc["content"] for doc in result["docs"] if doc["path"] not in indexes)
        )
        candidates = {
            "README.md",
            "pyproject.toml",
            "package.json",
            "requirements.txt",
            "Cargo.toml",
            "go.mod",
        } & paths
        for path in paths:
            if (
                path not in doc_paths
                and path in references_text
                and PurePosixPath(path).suffix
                in (
                    ".py",
                    ".ts",
                    ".tsx",
                    ".js",
                    ".jsx",
                    ".md",
                    ".json",
                    ".yaml",
                    ".yml",
                    ".toml",
                )
            ):
                candidates.add(path)
        # Include directly imported local Python/JS modules as surrounding source.
        for source in result["sources"]:
            for module in re.findall(
                r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)",
                source["content"],
                re.MULTILINE,
            ):
                module_path = module.replace(".", "/")
                candidates.update({module_path + ".py", module_path + "/__init__.py"} & paths)
            for module in re.findall(
                r'(?:from\s+|require\([\s]*|import\s*)[\'"]([.][^\'"]+)',
                source["content"],
            ):
                stem = posixpath.normpath(posixpath.join(posixpath.dirname(source["path"]), module))
                candidates.update(
                    {
                        stem,
                        *(
                            stem + suffix
                            for suffix in (
                                ".js",
                                ".ts",
                                ".jsx",
                                ".tsx",
                                "/index.js",
                                "/index.ts",
                            )
                        ),
                    }
                    & paths
                )
        if len(candidates | existing) > MAX_SOURCE_FILES * 2:
            raise IntegrationError("Referenced source exceeds supported context size.")
        for path in sorted(candidates - existing):
            result["sources"].append(_source(github, path, job.head_sha))
            bound()
        # REST files use current head: verify again after pagination and reads.
        if github.pull(job.pr.number)["head"]["sha"] != job.head_sha:
            raise IntegrationError("The pull request changed while gathering evidence.")
        bound()
        result["complete"] = True
    except IntegrationError as error:
        result["reason"] = str(error)
    except (KeyError, TypeError, ValueError, AttributeError):
        result["reason"] = "Required review context is malformed or unavailable."
    if (
        not result["complete"]
        and len(json.dumps(result, ensure_ascii=False).encode()) > MAX_CONTEXT_BYTES
    ):
        # Never hand an accidentally over-budget partial payload to a provider.
        return {
            "complete": False,
            "reason": result["reason"],
            "pr": {},
            "head_sha": job.head_sha,
            "files": [],
            "issues": [],
            "related_prs": [],
            "threads": [],
            "docs": [],
            "sources": [],
            "tasks": [],
        }
    return result
