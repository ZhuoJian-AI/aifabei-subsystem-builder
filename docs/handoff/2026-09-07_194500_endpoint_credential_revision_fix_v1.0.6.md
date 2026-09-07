# Endpoint credential revision fix v1.0.6

## Task

- Make endpoint acceptance compare the supported contract revision with the matching Runtime credential revision.

## Changes

- Corrected the endpoint validator's internal credential labels from `v2.5/v2.4` to `2.5/2.4`.
- Added a regression check and bumped only the Skill release version to `1.0.6`.
- The subsystem contract remains `2.5`; no credential, Runtime, SaaS, or business data was changed by this repository fix.

## Verification

- Focused tests: `14 passed`.
- Full suite: `167 passed, 41 skipped`.
- `quick_validate.py .`: `Skill is valid!`.
- Release package verification and public no-login updater verification remain release-time gates.

## Deployment

- After release, install the immutable `v1.0.6` package on the target ECS and rerun endpoint and end-to-end acceptance.
