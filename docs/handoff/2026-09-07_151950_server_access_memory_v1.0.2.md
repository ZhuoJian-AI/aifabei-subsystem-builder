# Server access memory v1.0.2 handoff

## Task

- Treat a novice's first server IP, root account, and password submission as durable authorization for that server.
- Reuse access across Codex tasks without asking the novice for credentials again.

## Changes

- Added a short root-Skill rule that makes server credentials one-time input and routes all later logins through durable access memory.
- Added `references/server-access-memory.md` with the first-login, key-installation, verification, reuse, failure, and revocation behavior.
- Added `scripts/server_access_memory.py` to create a per-server Ed25519 key, verify key-only login before committing a local profile, resolve saved access, and launch later logins without password fallback.
- Extended HTTP-proxy SSH support to use the dedicated identity in batch mode while preserving the original password-only first-login path.
- Updated administrator, ECS, VPN, README, release packaging, and updater requirements consistently.
- Bumped only `skillVersion` to `1.0.2`; subsystem contract revisions remain `2.4` and `2.5` with `2.5` as the default for new systems.

## Verification

- Focused tests: `22 passed` for access memory, updater, and version separation.
- Full suite with a D-drive temporary directory: `160 passed, 41 skipped`.
- Skill validator: `Skill is valid!`.
- Real local `ssh-keygen` smoke test created one dedicated private/public key pair and did not create a profile before key-login verification.

## Boundaries

- No live server, SaaS, Runtime, business system, database, or domain was changed.
- Initial passwords are not written to the profile, repository, command arguments, environment files, logs, or release assets.
- Stable GitHub Release and public `1.0.1 -> 1.0.2` upgrade verification follow after this change is merged to `main`.
- Repository release rules were fetched at `c948cd2`.
