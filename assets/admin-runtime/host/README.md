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
credential.

Because `/run` is cleared at every reboot, the installer also adds a
`systemd-tmpfiles` rule that recreates `/run/zhuojian` before Docker restores
managed containers. Docker requests one immediate disk check before restoring
containers, while a failed check cannot block unrelated legacy containers. This
keeps the read-only storage-gate mount current after a full server restart.

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
zhuojian-runtime rollback <applicationSlug> [--commit <full-sha>]
zhuojian-runtime backup <applicationSlug>
zhuojian-runtime restore <applicationSlug> --archive <exact-path> --confirm-application <applicationSlug>
```

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
`/etc/zhuojian/apps/<applicationSlug>.storage.env`; this host tool never opens or
prints that file. OSS applications join only the `zhuojian-storage` Docker
network and receive the storage env as a second Docker `--env-file`.

Applications are accepted only from
`/srv/zhuojian/repositories/aifabei-<applicationSlug>`, with a clean Git worktree
and a full commit SHA. Containers bind only `127.0.0.1:18000-18999`, use immutable
SHA image tags, fixed `/data`, a mode-`0600` app env, exact Host routing, health
gates, and an exact previous-image rollback. The tool refuses same-name resources
that it did not create and never runs Docker prune or deletes unknown volumes.

The daily backup timer briefly stops only containers carrying this tool's exact
ownership labels, then archives the module's local data and recovery
configuration. For `local-managed` releases this includes the database and local
files at one recovery point. For `oss-gateway` releases it does **not** copy OSS
objects or create a point-in-time OSS snapshot, so administrators must maintain
and test a separate object-protection/reconciliation plan. Local backups do not
replace an ECS snapshot or an administrator-selected off-server backup.
