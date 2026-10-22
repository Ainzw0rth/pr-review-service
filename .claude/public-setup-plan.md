# Public setup and repository management

Approved scope: Docker Compose first on local computers and VPS; finish Settings and repository Add/Edit/Pause, generic first-run setup, public documentation. Preserve existing production configuration. Defer unrelated Workflow-editor WIP.

## Design

Keep Django templates and existing design tokens/motion. Settings gets a visible navigation entry, repository list, empty state, Add/Edit and explicit connection feedback. Credentials are write-only/encrypted. Backend owner authorization, CSRF, safe retry, and stale-edit checks remain mandatory. Add repository fields with a fresh migration after 0024, never copy the old conflicting WIP migration.

Docker Compose plus PostgreSQL is the recommended runtime for both local and server installations. Environment initialization generates private secrets without overwriting existing files. Explicit loopback HTTP mode works without DEBUG; HTTPS/proxy settings remain secure for servers. Initial administrator/workspace bootstrap uses generic configurable identity and explicit owner binding. Local webhook ingress requires a public forwarding URL; document this rather than claiming localhost is reachable by GitHub.

README becomes a short generic entrypoint. Guides separate installation, configuration/provider setup, deployment/updates, optional integrations, and troubleshooting. No dependency on Louis's domains, repository, email, or filesystem paths. Preserve useful technical guidance but remove obsolete statements.

## Work items

- [x] Port only Settings/repository UI and backend, add missing empty/error/security cases, meaningful tests.
- [x] Implement generic environment initialization, local/HTTPS behavior, PostgreSQL defaults, and bootstrap; test clean install and no-overwrite behavior.
- [x] Rewrite public documentation against actual commands and supported platform boundaries.
- [x] Integrate, run suite and migration checks; exercise isolated PostgreSQL fresh setup.
- [x] Inspect rendered desktop/mobile UI, keyboard/form errors, reduced motion and add/edit/pause flow.
- [x] Independent code review, fix findings, record verification; merge only verified changes into main.

Runtime: Python/Django, PostgreSQL, Docker Compose, native HTML/CSS/JS. No new application dependencies planned. Current production environment and data stay untouched during development.

## Verification

- Full Django suite: 792 tests, 786 passed and 6 skipped.
- PostgreSQL targeted suite: 42 passed (repository UI, bootstrap, history and GitHub recovery).
- Fresh isolated Compose/PostgreSQL startup succeeded on Linux; `/login/` and health reachable over intentional localhost HTTP without DEBUG.
- Both validated localhost and HTTPS server configurations pass `check --deploy --fail-level WARNING`.
- Browser: password setup then login; empty state; invalid repository/reviewer errors; add/edit with masked token; pause/resume; capacity save; keyboard focus; desktop 1440px/mobile 390px without horizontal overflow; reduced-motion mode; no JavaScript errors.
- Reviewer findings fixed: redact token POST in exception reports; new repos default graph off. Additional regression: reject invalid webhook origins before API calls. PostgreSQL-specific test fixture ID assumption corrected.
- Lint, formatting, migration drift and whitespace checks passed. Live external GitHub registration/model review not run; no external test credentials provided.
- Runtime environment under this worktree belongs only to the isolated `ar-public-setup-check` test stack, never production.
