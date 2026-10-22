"""Exact-input reuse across provider fallback and explicit retry ancestry only."""

import hashlib
import json
from datetime import timedelta

from cryptography.fernet import InvalidToken
from django.utils import timezone

from .claims import fenced
from .models import Job, PhaseCheckpoint
from .providers import ProviderError, review_contract
from .review_agents import ACTIVE_AGENTS, selected
from .review_team import validate_report
from .review_v2 import validate_intent
from .secrets import decrypt, encrypt


def digest(value):
    encoder = json.JSONEncoder(sort_keys=True, separators=(",", ":"), allow_nan=False)
    result = hashlib.sha256()
    for chunk in encoder.iterencode(value):
        result.update(chunk.encode())
    return result.hexdigest()


@fenced()
def persist(job, phase, fingerprint, payload):
    PhaseCheckpoint.objects.update_or_create(
        job=job,
        phase=phase,
        defaults={
            "fingerprint": fingerprint,
            "encrypted_payload": encrypt(json.dumps(payload)),
            "created_at": timezone.now(),
        },
    )


class PhaseCheckpoints:
    def __init__(self, job, context):
        self.job = job
        self.context = context
        self.sources = {}
        self.phases = [row["key"] for row in selected(job.review_agents)]
        token = ACTIVE_AGENTS.set(job.review_agents)
        try:
            contracts = {
                task: review_contract(task)[:2]
                for task in ["intent", "review_synthesis", *[f"review_{p}" for p in self.phases]]
            }
        finally:
            ACTIVE_AGENTS.reset(token)
        self.base = digest(
            {
                "version": 1,
                "head": job.head_sha,
                "context": context,
                "workflow": [job.workflow_version, job.workflow_revision, job.workflow_steps],
                "agents": job.review_agents,
                "providers": job.provider_plan,
                "contracts": contracts,
            }
        )
        self.ancestors = [job.pk]
        parent = job.retry_of_id
        # Bounded, same-PR ancestry: never borrow from an unrelated human review cycle.
        while parent and parent not in self.ancestors and len(self.ancestors) < 100:
            row = Job.objects.filter(pk=parent, pr_id=job.pr_id, head_sha=job.head_sha).first()
            if row is None:
                break
            self.ancestors.append(row.pk)
            parent = row.retry_of_id

    def fingerprint(self, phase, intent):
        if phase not in ["intent", *self.phases]:
            raise ValueError("Unknown checkpoint phase")
        return digest([self.base, phase, intent])

    def validate(self, phase, result, intent):
        if phase == "intent":
            return validate_intent(result, self.context)
        return validate_report(result, {**self.context, "intent": intent})

    def load(self, phase, intent=None):
        fingerprint = self.fingerprint(phase, intent)
        rows = PhaseCheckpoint.objects.filter(
            job_id__in=self.ancestors,
            fingerprint=fingerprint,
            phase=phase,
            created_at__gte=timezone.now() - timedelta(days=90),
        ).order_by("-created_at")
        for row in rows:
            try:
                payload = json.loads(decrypt(row.encrypted_payload))
                result = self.validate(phase, payload["result"], intent)
                if phase != "intent" and result["complete"] is not True:
                    continue
                source = payload["source"]
                if not isinstance(source, dict):
                    continue
            except (InvalidToken, ValueError, KeyError, TypeError, ProviderError):
                continue
            self.sources[phase] = source
            return result
        return None

    def save(self, phase, result, provider, intent=None):
        result = self.validate(phase, result, intent)
        if phase != "intent" and result["complete"] is not True:
            return
        source = {
            "job": str(self.job.pk),
            "provider": provider.pk,
            "model": provider.model,
            "kind": provider.kind,
        }
        persist(
            self.job, phase, self.fingerprint(phase, intent), {"result": result, "source": source}
        )
        self.sources[phase] = source
