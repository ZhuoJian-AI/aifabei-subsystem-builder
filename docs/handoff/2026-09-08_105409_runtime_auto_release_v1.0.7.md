# Runtime automatic activation contract v1.0.7 handoff

## Task

- Keep the beginner-facing Skill flow unchanged while defining automatic Runtime release activation, system-developer access and persistent administrator stop boundaries.

## Changed behavior

- A valid latest Runtime-managed Manifest automatically becomes active; ordinary releases no longer wait for per-release administrator approval.
- New applications automatically grant only the platform-managed system developer role. Existing application grants inherit new modules, pages and Actions only inside their current ceiling.
- Administrator application/Action stops and explicit resource removals survive later Runtime synchronization.
- Invalid, stale or unhealthy candidates leave the previous healthy release active. Event declarations never create delivery routes.
- `publish_subsystem.py` treats only `healthy` as a successful platform registration state.
- Skill release version is `1.0.7`; the subsystem contract remains `2.5`.

## Verification

- `python -m pytest -q`: `170 passed, 41 skipped in 19.54s`.
- Skill Creator `quick_validate.py .`: `Skill is valid!`.
- `git diff --check`: passed.
- Release packaging is intentionally deferred until the change is merged to a clean `main`, because the builder correctly rejects a dirty worktree.

## Decisions and risks

- The top-level `SKILL.md` remains short. Detailed compatibility, inheritance and failure semantics live in the conditional references and automated tests.
- This Skill change does not itself authorize or perform a SaaS or ECS deployment.

## Remaining work

- Merge after integrating the latest `origin/main`, build and verify the immutable `v1.0.7` release assets, publish the GitHub Release, and run a public no-login update check.
