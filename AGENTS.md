# Repository Guide

## Read first

1. Read `SKILL.md` and `skill-version.json`.
2. For updater or release work, read `references/skill-updates.md`.
3. Check `TASKS.md` and the newest file in `docs/handoff/`.

## Commands

```text
python -m pytest -q
python C:/Users/王鑫涛/.codex/skills/.system/skill-creator/scripts/quick_validate.py .
python scripts/build_release.py --output-dir <temporary-directory>
```

## Hard boundaries

- `skillVersion` versions this repository; `contractRevision` versions SaaS integration. Never copy one into the other.
- New subsystems default to contract `2.5`; ordinary maintenance preserves an existing supported revision.
- Stable updates come from immutable GitHub Releases, never from `main`.
- Do not place credentials, local environment files, release archives, or generated caches in Git.
- Changes here do not authorize SaaS, ECS Runtime, or business-system deployments.

## Workflow

- Use a dedicated worktree and feature branch; never implement directly on `main`.
- Claim the task in `TASKS.md`, stage explicit files, run the relevant tests, and open a PR.
- Updater, release builder, version metadata, and contract migration logic are single-flight areas.
- Every completed change adds one file under `docs/handoff/` with commands and results.
- A stable release requires a clean merged `main`, a versioned changelog entry, full tests, package verification, and a public no-login install check.

## Layout

- `SKILL.md`: short user/agent entrypoint.
- `references/`: conditional operational and contract details.
- `scripts/`: deterministic validation, publishing, and update helpers.
- `schemas/`: supported subsystem contract schemas.
- `assets/`: generated subsystem and administrator Runtime templates.
- `tests/`: behavior and security regression tests.

