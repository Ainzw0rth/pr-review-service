# Distributed workers — approved design

Approved in conversation on 2026-10-26. Implemented and deployed; see distributed-workers-rollout.md for evidence and operational limits.

## Goal and boundaries

One repository, standalone installation by default, optional remote execution agents. Contabo remains coordinator and local execution host; InstaBox supplies one remote review slot initially. Keep PostgreSQL and Docker Compose. No Kubernetes, Redis, shared filesystem, fleet provisioning or coordinator/database HA.

Coordinator owns intake, durable PR claims, evidence preparation, orchestration, checkpoints, provider-account limits and GitHub publication. Agents own isolated model/test/graph execution, local authentication, session files and cleanup. Remote agents receive no coordinator database credentials, encryption keys or GitHub publishing credentials. Preserve current local installation without mandatory public endpoints.

Extract an execution boundary; do not run the existing database-coupled review_worker remotely. Existing same-host flock recovery remains valid only for coordinator/local resources, never proof of remote process death. Keep one durable PR claim across its remote phase assignments.

## Enrollment and protocol

Owner-only pairing: expiring single-use code exchanged for a worker-specific revocable credential. Outbound HTTPS from workers; no inbound worker port. Public coordinator endpoint is an explicit prerequisite for remote mode. Versioned, validated protocol with bounded payloads, authenticated artifact transfer and scoped worker access.

Coordinator assigns immutable work with assignment token, generation, worker incarnation, provider account group, capability and session placement. Tokens fence progress/results. Completion is idempotent; conflicting repeats fail. Workers cannot assign themselves arbitrary repositories or accounts. Artifacts are authorized per assignment, integrity checked and bounded by existing archive limits.

Shared accounts require global admission, including local execution and probes. One remote review slot does not imply unlimited model sessions. Preserve existing authentication-coordination protections; do not copy rotating CLI credentials between hosts. Independently authenticate agents and map their accounts explicitly.

Lost contact means unreachable, not terminated. Reject stale results; quarantine uncertain execution until cleanup is acknowledged or termination established. Do not release scarce account permits merely because a lease expired. Coordinator alone publishes, preserving existing publication reconciliation; do not promise exactly-once external writes across GitHub/network failures.

## Health and keepalive

Worker heartbeat: 15 seconds. Stale after 60 seconds, no new assignments. Assignment leases and execution cleanup are separate from display freshness. Dashboard refresh: 10 seconds while visible, no overlapping fetches.

External keepalive: configurable per worker, enabled every 20 minutes for InstaBox. Run from always-on coordinator, not browser or worker. Fixed authenticated SSH command through verified host key; no user-supplied shell snippets. Private credential storage, redacted errors, timeouts, bounded retries, persisted due times and exclusive claims. Execute overdue checks after restart. Successful SSH is connectivity only: fresh authenticated worker heartbeat and capability/auth/resource checks establish Ready.

Verify experimentally across multiple 30-minute idle windows that SSH activity prevents InstaBox suspension. If it only resumes, report that distinction. Never guarantee uninterrupted uptime. On-demand wake remains available when work needs an offline worker. Generic hosts need no provider integration. Credential expiry/revocation is actionable, not an infinite retry loop.

## Worker UI

Owner-only Workers navigation beside Workflow and Settings; show local and remote workers. Overview: connected count, available/occupied slots, active work, attention count. Detail: availability, readiness problems, provider capabilities, active PR/phase, memory, working-disk free space, version, last heartbeat, keepalive result and next check.

Availability: Connecting, Ready, Busy, Draining, Offline. Show Paused only with provider evidence. Readiness reasons: authentication required, disk pressure, incompatible version, recovery required. Unknown metrics stay unknown, never zero or healthy. Actions: Add worker, Drain, Resume scheduling, Check now, Configure keepalive, Revoke access. Draining lets current work finish; revocation fences results but does not assert process termination.

Reuse existing Django templates and vanilla JavaScript. Martian Mono; paper #f6f4ee, panel #fdfbf6, ink #222220; existing muted status colors, 8px radius, 44px controls, typography, spacing and motion tokens. Compact desktop table, mobile summaries; existing details/disclosure patterns. Purposeful transitions, reduced-motion handling, semantic controls, visible focus and text status labels. Poll updates preserve focus and expanded rows. Cover loading, empty, stale, error and success states. Verify 375/768/1440px and keyboard flows.

## Installation, updates and rollout

Existing setup remains default. Worker-only option asks coordinator URL, pairing code and worker name, checks Docker/platform/disk, pulls pinned images, creates private persistent configuration, guides provider login, starts agent and verifies enrollment/readiness. Resume interrupted setup without rotating valid credentials or duplicating registration. compose.worker.yaml starts no database. Versioned prebuilt images for verified architectures; retain source builds for development.

Deploy tooling must account for remote assignments and distributed drain acknowledgement before schema/version changes. Rollback disables remote scheduling and fences assignments without silently discarding uncertain execution. Remote unavailability must not unnecessarily block unrelated local reviews, except where shared account permits remain uncertain.

Verification: standalone regression; enrollment/security; concurrent ownership; stale and duplicate results; crashes/disconnects/reboots; global limits and sessions; disk pressure; keepalive/pause behavior; UI interaction. First remote review uses nonpublishing validation, then one live slot on a designated test repository after deployment authorization. Keep active production work intact.
