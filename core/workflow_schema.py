"""Per-attempt workflow identity and planned dashboard stages, not an execution engine."""

from .graph import enabled
from .review_agents import selected

# Bump for review-step or behavior changes. Keep old stage IDs/renderers available.
REVISION = "v3.1"
TEAM_REVISIONS = {"v3", "v3.1"}
GROUNDED_REVISIONS = {"v2", "v2.1", *TEAM_REVISIONS}

# Fixed pre-versioning vocabulary: never add future stages to legacy attempts.
LEGACY_STEPS = ("files", "discussion", "docs", "graph", "checks", "tests", "publication", "sheet")


def snapshot(repository, agents=None):
    steps = ["files", "discussion", "docs"]
    if enabled(repository):
        steps.append("graph")
    steps.extend(["intent", "checks"])
    if repository.test_command:
        steps.append("tests")
    if REVISION in TEAM_REVISIONS:
        steps.extend(
            {"code": "code_review", "security": "security_review", "tests": "test_review"}.get(
                row["key"], row["key"] + "_review"
            )
            for row in selected(agents)
        )
        steps.append("synthesis")
    steps.extend(
        [
            "pr_summary",
            "inline_findings",
            "discussion_replies",
            "related_pr_comments",
            "related_issue_comments",
            "labels",
        ]
    )
    if repository.sheet_config:
        steps.append("sheet")
    return REVISION, steps
