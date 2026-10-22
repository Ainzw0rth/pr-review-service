"""Export public CLI commentary and evidence-tool metadata, never raw streams/results."""

import re

CODES = {
    "cli_output_text",
    "cli_output_tool",
    "cli_output_done",
    "cli_output_failed",
    "cli_output_status",
}


def public_text(value):
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value)
    value = re.sub(r"[\x00-\x1f\x7f]", " ", value)
    value = re.sub(r"https?://\S+", "[link]", value)
    value = re.sub(r"\b(?:sk-|gh[pousr]_|github_pat_)[A-Za-z0-9_-]{12,}", "[redacted]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", "[redacted]", value)
    value = re.sub(
        r"(?i)\b(?:bearer\s+|(?:password|secret|api[_-]?key|token)\s*[:=]\s*)[^\s,;]+",
        "[redacted]",
        value,
    )
    return value[:800]


class Transcript:
    def __init__(self, kind, context, emit):
        self.kind, self.emit = kind, emit
        self.tools = {}
        self.last_comment = None
        self.names = {}
        for section, value in context.items():
            if isinstance(value, list):
                for index, entry in enumerate(value):
                    key = f"{section}_{index}"
                    path = (
                        (entry.get("filename") or entry.get("path"))
                        if isinstance(entry, dict)
                        else None
                    )
                    self.names[key] = (
                        public_text(path) if isinstance(path, str) and len(path) <= 180 else key
                    )
            else:
                self.names[section] = section

    def commentary(self, text):
        if not isinstance(text, str):
            return
        match = re.fullmatch(r"\[review-progress\][ \t]+([^\r\n]{1,600})", text.strip())
        if not match:
            return
        value = public_text(match[1])
        if value != self.last_comment:
            self.emit("cli_output_text", value)
            self.last_comment = value

    def start(self, identifier, arguments):
        if not isinstance(identifier, str) or len(identifier) > 200 or identifier in self.tools:
            return
        if not isinstance(arguments, dict):
            return
        reads = arguments.get("reads")
        if not isinstance(reads, list) or not 1 <= len(reads) <= 3:
            return
        names = []
        for read in reads:
            if (
                not isinstance(read, dict)
                or not isinstance(read.get("id"), str)
                or read["id"] not in self.names
            ):
                return
            offset, length = read.get("offset", 0), read.get("length", 20000)
            if (
                type(offset) is not int
                or type(length) is not int
                or not 0 <= offset <= 134217728
                or not 1 <= length <= 20000
            ):
                return
            names.append(f"{self.names[read['id']]} (offset {offset}, up to {length} characters)")
        label = public_text("read_evidence · " + "; ".join(names))
        self.tools[identifier] = label
        # Tool calls are sequential/bounded by the native client; retain only in-flight metadata.
        if len(self.tools) > 100:
            self.tools.pop(next(iter(self.tools)))
        self.emit("cli_output_tool", "Started · " + label)

    def finish(self, identifier, failed=False):
        if not isinstance(identifier, str):
            return
        label = self.tools.pop(identifier, None)
        if label:
            self.emit(
                "cli_output_failed" if failed else "cli_output_done",
                ("Failed · " if failed else "Completed · ") + label,
            )

    def feed(self, event):
        if not self.emit or not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "thread.started" or kind == "system" and event.get("subtype") == "init":
            self.emit("cli_output_status", "CLI session started.")
        elif kind == "turn.completed" or kind == "result" and event.get("subtype") == "success":
            self.emit(
                "cli_output_status",
                "CLI response completed; final review withheld from this transcript.",
            )
        elif kind in {"turn.failed", "runner_error"} or kind == "result" and event.get("is_error"):
            self.emit(
                "cli_output_status",
                "CLI attempt ended with an error. See the model status for its classified reason.",
            )
        elif kind == "stream_event":
            body = event.get("event")
            delta = body.get("delta") if isinstance(body, dict) else None
            if isinstance(delta, dict) and delta.get("stop_reason") == "refusal":
                self.emit("cli_output_status", "CLI reported a refusal; this attempt is stopping.")
        if self.kind == "codex_cli" and kind in {"item.started", "item.completed"}:
            item = event.get("item")
            if not isinstance(item, dict):
                return
            if item.get("type") == "agent_message" and kind == "item.completed":
                self.commentary(item.get("text"))
            elif (
                item.get("type") == "mcp_tool_call"
                and item.get("server") == "review_context"
                and item.get("tool") == "read_evidence"
            ):
                self.start(item.get("id"), item.get("arguments"))
                if kind == "item.completed":
                    self.finish(
                        item.get("id"), item.get("status") == "failed" or bool(item.get("error"))
                    )
        elif self.kind == "claude_cli" and kind in {"assistant", "user"}:
            message = event.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if not isinstance(content, list):
                return
            for block in content:
                if not isinstance(block, dict):
                    continue
                if kind == "assistant" and block.get("type") == "text":
                    self.commentary(block.get("text"))
                elif (
                    kind == "assistant"
                    and block.get("type") == "tool_use"
                    and block.get("name") == "mcp__review_context__read_evidence"
                ):
                    self.start(block.get("id"), block.get("input"))
                elif kind == "user" and block.get("type") == "tool_result":
                    self.finish(block.get("tool_use_id"), bool(block.get("is_error")))
