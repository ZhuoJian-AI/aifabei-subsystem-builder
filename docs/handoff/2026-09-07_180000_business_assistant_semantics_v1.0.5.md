# Business assistant semantics v1.0.5 handoff

## Owner and task

- Owner: Codex
- Task: `SKILL-BUSINESS-SEMANTICS-001`

## Changed behavior

- Kept the Skill entrypoint to one additional result rule and moved semantic-map details into the v2.5 platform contract.
- Added optional schema support and required v2.5 source validation for page `aiSemantics`, including valid related pages and a real default query Action.
- Requires closed, non-empty AI Action schemas, bounded query/export input and the standard paged export dataset.
- Endpoint/source acceptance now reports subsystem contract, SaaS format capability and SaaS artifact end-to-end checks separately.
- Scaffolded v2.5 subsystems now include a minimal semantic declaration and bounded Action schema.

## Verification

- Full suite before main synchronization: 166 passed, 41 skipped, 23 subtests passed.
- Skill Creator `quick_validate.py`: `Skill is valid!`.
- `git diff --check`: passed.

## Remaining release work

- Rebuild from a clean merged main, publish immutable `v1.0.5` assets and perform the public no-login update check.

## Decisions and risks

- Contract revision remains `2.5`; only the Skill release version becomes `1.0.5`.
- Existing registered applications without semantics remain migration warnings; new/updated v2.5 source manifests must pass the stronger checks.
