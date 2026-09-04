# ZhuoJian ECS administrator foundation

This bundle installs a controlled direct-deployment entry for one already
provisioned Alphabet Runtime. It does **not** create, rotate, display, or copy the
Runtime registration credential, and it does not adopt or modify existing
applications.

When the separate company OSS gateway has been installed and accepted, this
same host tool can atomically make `oss-gateway` the default for future
applications. Existing releases keep their recorded storage backend; a normal
deploy never performs a file migration.

## Install

Run as root from this directory:

```sh
./install.sh
```

This preserves the management path already verified by
`provision_runtime.py`. Only after an administrator has separately installed
and tested SSH/HTTPS multiplexing on port 443, use:

```sh
./install.sh --enable-ssh-https-multiplex \
  --public-address <ECS-public-address>
```

The installer requires the existing `/etc/zhuojian/runtime.json` and root-owned
mode-`0600` `/etc/zhuojian/runtime-registration.key`. It atomically augments only
the explicitly requested SSH/HTTPS-443 management fields, preserves a verified
standard-SSH profile by default, and never reads or rotates the Runtime
credential. Existing managed directories must be real root-owned directories;
the installer refuses symlinks or foreign ownership and reapplies each fixed
mode. Its default Nginx guard follows the resulting Runtime profile exactly:
public `443` for `standard-ssh`, or loopback `8443` when public `443` is owned by
the separately verified SSH/HTTPS multiplexer.

Because `/run` is cleared at every reboot, the installer also adds a
`systemd-tmpfiles` rule that recreates `/run/zhuojian` and the stable,
root-owned mode-`0444` `/run/zhuojian/upload.lock` inode before Docker restores
managed containers. Every managed application receives that directory as a
read-only mount and uses the same `flock`, so disk-consuming uploads from
different future systems cannot all pass the 5 GiB reserve check concurrently.
Docker requests one immediate disk check before restoring containers, while a
failed check cannot block unrelated legacy containers. This keeps the
read-only storage-gate mount and upload coordinator current after a full server
restart. `doctor`, deploy preflight and container start fail closed if the lock
is missing, replaced by a symlink, or has the wrong metadata.

## Controlled commands

```text
zhuojian-runtime doctor
zhuojian-runtime disk-check --write-state
zhuojian-runtime preflight <applicationSlug>
zhuojian-runtime ensure-app <applicationSlug>
zhuojian-runtime prepare <applicationSlug>
zhuojian-runtime certify <applicationSlug> [--email admin@example.com]
zhuojian-runtime deploy <applicationSlug> --issue-certificate
zhuojian-runtime status <applicationSlug>
zhuojian-runtime verify-release <applicationSlug>
zhuojian-runtime rotate-app-storage <applicationSlug> --grace-seconds 300
zhuojian-runtime rollback <applicationSlug> [--commit <full-sha>]
zhuojian-runtime backup <applicationSlug>
zhuojian-runtime restore <applicationSlug> --archive <exact-path> --confirm-application <applicationSlug>
```

`deploy` and `rollback` close the matching SaaS release before switching the
container and return `awaiting_platform_registration`. Run
`scripts/publish_subsystem.py` immediately afterward (`--use-running-release`
after rollback). The root-only Runtime credential is used only for this gate
and is never printed or passed to Docker.

Every code switch first records a durable `releaseSwitch` transaction in
`release.json`, including the exact old/new commit images, deterministic
rollback container, and previous Nginx content. Any later command for that
application restores the old container and Nginx after an interrupted switch,
checks the old health endpoint, and only then cancels the matching SaaS intent.
The marker is retained whenever Docker, Nginx, or SaaS cannot be reconciled, so
employee access stays fail-closed across SIGKILL, daemon restart, and power loss.
Before publication, `verify-release` requires the canonical container to be
Running and requires its commit label and Docker `Config.Image` to match
`release.current` exactly.

After the gateway installer has created its private Docker network, root-only
management command and verified credential file, the administrator performs the
one-time in-place Runtime switch:

```text
zhuojian-runtime configure-oss-gateway \
  --bucket <company-private-bucket> \
  --region <aliyun-region-id>
```

This command preserves the existing Runtime publisher credential byte-for-byte,
then performs real OSS PUT/GET/DELETE and two-application isolation checks before
it changes the Runtime profile. `ensure-app`, `prepare`, and `deploy`
then ask the root-only gateway command to idempotently create the application's
isolated identity. The gateway writes the token straight to
`/etc/zhuojian/storage-apps/<applicationSlug>.storage.env`; this host tool never opens or
prints that file. During a host-first rolling upgrade it also accepts the former
`/etc/zhuojian/apps/<applicationSlug>.storage.env` location, but only when exactly
one of the two files exists and the release records that exact managed path. The
gateway migration atomically moves the file and updates the release; arbitrary
paths and duplicate old/new files are refused. The gateway keeps its Compose management network, while the
host tool creates a separately labeled `zhuojian-storage-<applicationSlug>`
bridge for every OSS application and attaches only that application and the
gateway to it. Applications receive the storage env as a second Docker
`--env-file`; they never join the shared gateway management bridge or another
application's bridge.

Applications are accepted only from
`/srv/zhuojian/repositories/<enterpriseKey>-<applicationSlug>`, with a clean Git worktree
and a full commit SHA. Containers bind only `127.0.0.1:18000-18999`, use immutable
SHA image tags, fixed `/data`, a mode-`0600` app env, exact Host routing, health
gates, a `512m` Nginx request-body limit matching the managed application upload
limit, and an exact previous-image rollback. The app env receives the canonical,
validated HTTPS `platform.baseUrl` as `ZHUOJIAN_SAAS_ORIGINS`; existing managed
env files are atomically repaired with the exact application origin and shared
upload-lock path without printing their secrets. The tool refuses same-name resources
that it did not create and never runs Docker prune or deletes unknown volumes.

`rotate-app-storage` is the crash-resumable online rotation operation for a
deployed OSS application. Before contacting the gateway it persists a random
operation id, exact frozen image and deterministic rollback-container name in
`release.json`. Gateway prepare installs the new token without expiring the old
one. Runtime labels the replacement with that operation id, recreates it from the
frozen image, and health-checks it; only then does gateway commit start the old
token's grace interval. Every prepare and commit is idempotent. If a response is
lost, the process is killed, the host reboots between stop/rename/start, or commit
finishes before its response arrives, rerunning the same Runtime command converges
from the persisted marker. A pre-commit health failure restores the old container,
whose token remains current without a deadline, and leaves the operation ready to
retry. Deploy, rollback and restore refuse to run while such a marker is pending.
The gateway's compatibility `rotate` command is not a complete online workflow.

The daily backup timer briefly stops only containers carrying this tool's exact
ownership labels, then archives the module's local data and recovery
configuration. For `local-managed` releases this includes the database and local
files at one recovery point. For `oss-gateway` releases it does **not** copy OSS
objects or create a point-in-time OSS snapshot, so administrators must maintain
and test a separate object-protection/reconciliation plan. Local backups do not
replace an ECS snapshot or an administrator-selected off-server backup.
