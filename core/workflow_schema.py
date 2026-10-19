"""Per-attempt workflow identity and planned dashboard stages, not an execution engine."""

from django.conf import settings

# Bump for review-step or behavior changes. Keep old stage IDs/renderers available.
REVISION = "v3"
TEAM_REVISIONS = {"v3"}
GROUNDED_REVISIONS = {"v2", "v2.1", *TEAM_REVISIONS}

# Fixed pre-versioning vocabulary: never add future stages to legacy attempts.
LEGACY_STEPS = ("files", "discussion", "docs", "graph", "checks", "tests", "publication", "sheet")


def snapshot(repository):
    steps = ["files", "discussion", "docs"]
    if repository.full_name in settings.GRAPH_REPOSITORIES:
        steps.append("graph")
    steps.extend(["intent", "checks"])
    if repository.test_command:
        steps.append("tests")
    if REVISION in TEAM_REVISIONS:
        steps.extend(["code_review", "security_review", "test_review", "synthesis"])
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
