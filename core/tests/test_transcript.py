from unittest import TestCase


class TranscriptTests(TestCase):
    def make(self, kind="codex_cli"):
        from core.transcript import Transcript

        self.events = []
        return Transcript(
            kind,
            {"files": [{"filename": "src/auth.py"}]},
            lambda code, text: self.events.append((code, text)),
        )

    def test_only_explicit_public_commentary_is_exported(self):
        t = self.make()
        for kind, text in [
            ("reasoning", "[review-progress] hidden reasoning"),
            ("agent_message", '{"summary":"private findings"}'),
            ("agent_message", "unmarked final text"),
        ]:
            t.feed({"type": "item.completed", "item": {"type": kind, "text": text}})
        self.assertEqual(self.events, [])
        t.feed(
            {
                "type": "item.completed",
                "item": {
                    "type": "agent_message",
                    "text": "[review-progress] Checking src/auth.py against the linked issue.",
                },
            }
        )
        self.assertEqual(
            self.events, [("cli_output_text", "Checking src/auth.py against the linked issue.")]
        )

    def test_codex_tools_expose_paths_and_outcomes_not_results(self):
        t = self.make()
        item = {
            "id": "tool1",
            "type": "mcp_tool_call",
            "server": "review_context",
            "tool": "read_evidence",
            "arguments": {"reads": [{"id": "files_0", "offset": 0, "length": 100}]},
        }
        t.feed({"type": "item.started", "item": item})
        t.feed(
            {
                "type": "item.completed",
                "item": {**item, "status": "completed", "result": {"secret": "source contents"}},
            }
        )
        self.assertEqual([e[0] for e in self.events], ["cli_output_tool", "cli_output_done"])
        self.assertIn("src/auth.py", self.events[0][1])
        self.assertNotIn("source contents", str(self.events))

    def test_claude_message_tools_and_results_are_correlated(self):
        t = self.make("claude_cli")
        t.feed(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "private"},
                        {"type": "text", "text": "[review-progress] Checking the changed file."},
                        {
                            "type": "tool_use",
                            "id": "tool1",
                            "name": "mcp__review_context__read_evidence",
                            "input": {"reads": [{"id": "files_0"}]},
                        },
                    ]
                },
            }
        )
        t.feed(
            {
                "type": "user",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "tool1",
                            "content": "secret source",
                            "is_error": True,
                        }
                    ]
                },
            }
        )
        self.assertEqual(
            [e[0] for e in self.events], ["cli_output_text", "cli_output_tool", "cli_output_failed"]
        )
        self.assertNotIn("secret", str(self.events))
        self.assertNotIn("private", str(self.events))

    def test_redacts_credentials_and_rejects_multiline_output(self):
        t = self.make()
        for text in [
            "[review-progress] token=abc123 sk-abcdef01234567890123456789012345",
            "[review-progress] progress\nprivate final findings",
        ]:
            t.feed({"type": "item.completed", "item": {"type": "agent_message", "text": text}})
        self.assertEqual(len(self.events), 1)
        self.assertNotIn("abc123", str(self.events))
        self.assertNotIn("sk-abcdef", str(self.events))
        self.assertNotIn("private final", str(self.events))
