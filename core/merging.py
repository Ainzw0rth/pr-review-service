"""One-shot, commit-pinned merging after confirmed service approval.

Never schedule GitHub auto-merge: that could merge a future, unreviewed commit.
An intent receipt prevents blind replay after a lost response or process crash.
"""

from django.conf import settings

from .github import check_required
from .models import ApprovalRecord, Job, Receipt

MERGE_STATE = """query($owner:String!,$name:String!,$number:Int!){
  repository(owner:$owner,name:$name){pullRequest(number:$number){
    headRefOid mergeable mergeStateStatus reviewDecision
  }}
}"""


def merge_approved(github, job, outcome):
    if not settings.LIVE_WRITES or outcome != "ready":
        return
    row = Job.objects.select_related("pr__repository").get(pk=job.pk)
    repo = row.pr.repository
    if not (row.auto_merge and row.auto_approve and repo.auto_merge and repo.auto_approve):
        return
    if Receipt.objects.filter(job=row, key__in=["merge", "merge-intent"]).exists():
        return

    def note(message):
        # Preserve unrelated optional-integration warnings, while keeping the bounded reason.
        fresh = Job.objects.get(pk=row.pk)
        prefix = fresh.reason if fresh.reason and not fresh.reason.startswith("Auto-merge") else ""
        Job.objects.filter(pk=row.pk).update(
            reason=(message + (" " + prefix if prefix else ""))[:250]
        )

    try:
        if row.status != "completed" or row.generation != row.pr.generation or not repo.enabled:
            return
        if (
            not row.review_checkpoint.stage == "done"
            or row.review_checkpoint.state.get("review_event") != "APPROVE"
        ):
            note("Auto-merge skipped: no completed formal approval publication.")
            return
        record = (
            ApprovalRecord.objects.filter(
                job=row, dismissed_at__isnull=True, revoke_requested=False
            )
            .exclude(review_id="")
            .first()
        )
        if not record:
            note("Auto-merge skipped: service approval is missing or withdrawn.")
            return
        fresh = github.pull(row.pr.number)
        if fresh.get("merged"):
            note("Auto-merge skipped: PR was already merged.")
            return
        labels = {label["name"] for label in fresh.get("labels", [])}
        if (
            fresh.get("state") != "open"
            or fresh.get("draft", True)
            or fresh["head"]["sha"] != row.head_sha
            or repo.approved_label not in labels
            or repo.review_label in labels
            or repo.feedback_label in labels
        ):
            note("Auto-merge skipped: PR commit, state or workflow labels changed.")
            return
        if row.merge_method not in {"squash", "merge", "rebase"}:
            note("Auto-merge skipped: invalid merge method.")
            return
        if not check_required(github, repo, row.head_sha)["passed"]:
            note("Auto-merge skipped: required CI is missing, pending or failed.")
            return
        reviews = github.pages(f"/pulls/{row.pr.number}/reviews")
        if not any(
            str(review.get("id")) == record.review_id
            and review.get("state") == "APPROVED"
            and review.get("commit_id") == row.head_sha
            and (review.get("user") or {}).get("login", "").casefold() == record.actor.casefold()
            and record.marker in (review.get("body") or "")
            for review in reviews
        ):
            note("Auto-merge skipped: GitHub no longer confirms the service approval.")
            return
        owner, name = repo.full_name.split("/")
        state = github.graphql(
            MERGE_STATE, {"owner": owner, "name": name, "number": row.pr.number}
        )["repository"]["pullRequest"]
        if (
            state.get("headRefOid") != row.head_sha
            or state.get("mergeable") != "MERGEABLE"
            or state.get("mergeStateStatus") != "CLEAN"
            or state.get("reviewDecision") != "APPROVED"
        ):
            note(
                "Auto-merge skipped: GitHub has not confirmed clean mergeability and required approval. Check conflicts, reviews and branch rules."
            )
            return
        # Owner disable, cancellation or supersession during the network checks must win.
        if not Job.objects.filter(
            pk=row.pk,
            status="completed",
            auto_merge=True,
            pr__generation=row.generation,
            pr__repository__enabled=True,
            pr__repository__auto_merge=True,
            pr__repository__auto_approve=True,
        ).exists():
            return
        if not ApprovalRecord.objects.filter(
            pk=record.pk, dismissed_at__isnull=True, revoke_requested=False
        ).exists():
            return
        _, created = Receipt.objects.get_or_create(job=row, key="merge-intent")
        if not created:
            return
        note(
            "Auto-merge outcome could not be confirmed yet. Inspect GitHub before retrying; the merge request is not replayed automatically."
        )
        result = github.put(
            f"/pulls/{row.pr.number}/merge", {"sha": row.head_sha, "merge_method": row.merge_method}
        )
        if result.get("merged") is not True:
            note(
                "Auto-merge: GitHub did not merge the PR. Inspect branch rules, permissions and enabled merge methods; manual action required."
            )
            return
        Receipt.objects.get_or_create(
            job=row, key="merge", defaults={"remote_id": result.get("sha", "")}
        )
        note("Auto-merge completed: reviewed commit merged using " + row.merge_method + ".")
    except Exception:
        # Never expose transport secrets or convert a published review into a failed analysis.
        if Receipt.objects.filter(job=row, key="merge-intent").exists():
            note(
                "Auto-merge outcome could not be confirmed. Inspect GitHub before retrying; no automatic replay."
            )
        else:
            note(
                "Auto-merge checks unavailable. Review remains published; inspect GitHub or merge manually."
            )
