# Server source baseline v1.0.4 handoff

## Task

- When an existing server project is newer than, diverges from, or contains source changes missing from the business owner's local/Git copy, use the server source as the development baseline.
- Keep the novice-facing Skill concise and place synchronization safeguards in the deployment reference.

## Changes

- Updated the existing-project step in `SKILL.md` with the server-source-baseline rule and linked the detailed ECS deployment guidance.
- Defined read-only comparison before synchronization, isolated worktree use, divergence handling, explicit source-file review, and the narrow exception conditions in `references/direct-ecs-deployment.md`.
- Explicitly excluded secrets, databases, uploads, logs, caches, dependencies, and build outputs from source synchronization.
- Bumped only `skillVersion` to `1.0.4`; subsystem contract revisions remain `2.4` and `2.5`, with `2.5` still the default.

## Verification

- `python -m pytest -q`: `162 passed, 41 skipped`.
- `quick_validate.py .`: `Skill is valid!`.
- `git diff --check`: passed.

## Boundaries

- No SaaS platform, Runtime, business server, business repository, database, domain, or deployed system was changed.
- A running container's ad-hoc modifications are not automatically promoted to source; they must be reported and reviewed.
- Stable GitHub Release and public `1.0.3 -> 1.0.4` upgrade verification follow after this change is merged to `main`.
- Repository deployment rules were fetched at `c948cd2`.
