# ZhuoJian company OSS gateway

This service gives every subsystem an isolated view of one private Alibaba Cloud
OSS bucket. Applications only see relative object keys. The gateway derives the
real key as `apps/<application-slug>/<relative-key>` from a high-entropy Bearer
credential stored as a SHA-256 hash in SQLite. An application cannot submit or
override its slug or OSS prefix.

## Host layout

- `/etc/zhuojian/oss-gateway.env` — root-owned mode `0600`; the only file
  containing the company OSS AccessKey.
- `/var/lib/zhuojian-storage-gateway/registry.sqlite3` — root-owned mode `0600`;
  application state and token hashes only.
- `/etc/zhuojian/storage-apps/<slug>.storage.env` — root-owned mode `0600`; generated
  application credential consumed by Docker Compose as an `env_file`.
- Docker network `zhuojian-storage` — gateway management only; it is not
  published on a host or public port and application containers never join it.
- Docker network `zhuojian-storage-<slug>` — one Runtime-managed private bridge
  per application. Only that application's canonical container (or a stopped,
  exactly labeled rollback container) and the gateway may join it. Applications
  reach `http://zhuojian-storage-gateway:8080` through their own bridge.

The root secret file is a plain `KEY=VALUE` file and is never sourced as shell:

```dotenv
OSS_ENDPOINT=https://oss-cn-hongkong.aliyuncs.com
OSS_BUCKET=replace-with-private-company-bucket
OSS_ACCESS_KEY_ID=replace-on-server
OSS_ACCESS_KEY_SECRET=replace-on-server
```

Create it without putting credentials in command arguments or shell history,
then set its owner/mode to `root:root` and `0600`. The gateway refuses a symlink,
a non-root owner, or any group/other permission. It has no AccessKey environment
variable fallback.

## Install and manage

Installation is deliberately a two-step root operation so that an OSS AccessKey
never appears in command arguments, shell history, or installer output. First,
create `/etc/zhuojian/oss-gateway.env` exclusively, edit it with a root-only
editor, and verify its metadata:

```console
sudo install -d -o root -g root -m 0700 /etc/zhuojian
sudo python3 -c 'import os; p="/etc/zhuojian/oss-gateway.env"; f=os.open(p, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600); os.close(f); os.chown(p, 0, 0)'
sudoedit /etc/zhuojian/oss-gateway.env
sudo stat -c '%U:%G %a' /etc/zhuojian/oss-gateway.env
```

The final command must report `root:root 600`. If the file already exists, stop
and have an administrator identify it; do not truncate or replace it. Then run
the installer from this source directory:

```console
sudo sh ./install.sh
```

The installer targets a GNU/Linux ECS with Docker Compose v2 and the standard
coreutils, findutils, and util-linux tools. It is repeatable and always deploys runtime files to
`/opt/zhuojian/storage-gateway`. It creates the root-owned persistent registry,
spool, and application-environment directories; installs the root-only admin
wrapper at `/usr/local/sbin/zhuojian-storage-gateway-admin`; runs `docker compose
build` and `docker compose up`; and waits for both Docker health and `/healthz`.
It never reads the secret file in the host shell and never prints its contents.

An ownership marker prevents the installer from taking over a non-empty install
directory it did not create. On first installation it also refuses conflicting
wrapper, container, network, or image names that do not carry the expected
Compose identity. It does not prune images, remove volumes, delete objects, or
remove unrelated containers. Before legacy Secret migration begins, a failed
build, switch, network reattachment, or health check restores both the exact
previous Compose file and the previous healthy image. Migration is the explicit
forward-only boundary: after it may have atomically moved a Secret, the installer
keeps the new gateway/Compose pair and idempotently resumes migration instead of
combining the new Secret layout with an incompatible old image. Persistent
registry data is never rolled back; failed-build cache and a rejected image may
remain for diagnosis.

The install-time check is local service health; it does not claim that the OSS
policy or network path works. Complete the real object-storage acceptance probe
with the expected non-secret Bucket name and region ID:

```console
sudo zhuojian-storage-gateway-admin probe \
  --expected-bucket <company-private-bucket> \
  --expected-region <region-id>
```

The probe creates two temporary application identities, exercises real
put/get/delete operations and cross-application isolation, proves anonymous
reads of a real object are denied, and proves the RAM identity cannot
list/read/write/delete outside `apps/*`. It then revokes the temporary
credentials and removes their exact test objects. A successful probe therefore
means the live Bucket is private at the object boundary as well as correctly
scoped for the gateway.

The wrapper runs the management command inside the already-running private
gateway container, where the registry and application env directory are mounted.

Runtime provisioning calls this idempotent command:

```console
zhuojian-storage-gateway-admin ensure-app --application-slug example-app
```

It prints only the slug, state, env path, and whether the existing credential was
reused. It writes the credential directly to
`/etc/zhuojian/storage-apps/example-app.storage.env` using an atomic replace and mode
`0600`; it never returns the credential to the caller. The generated file uses
the v1 names `FILE_STORAGE_DRIVER=oss-gateway`,
`FILE_STORAGE_GATEWAY_URL`, and `FILE_STORAGE_TOKEN`. Two legacy aliases are
temporarily emitted for older templates.

Other root-only lifecycle commands are:

```console
# Compatibility-only one-shot operation; Runtime does not use this for online rotation.
zhuojian-storage-gateway-admin rotate --application-slug example-app --grace-seconds 300
zhuojian-storage-gateway-admin prepare-rotate --application-slug example-app --operation-id <durable-id> --grace-seconds 300
zhuojian-storage-gateway-admin commit-rotate --application-slug example-app --operation-id <same-durable-id>
zhuojian-storage-gateway-admin suspend --application-slug example-app
zhuojian-storage-gateway-admin resume --application-slug example-app
zhuojian-storage-gateway-admin revoke --application-slug example-app
```

Runtime performs online rotation as a durable two-phase operation. `prepare-rotate`
idempotently writes and registers the new token but does not expire any old token.
The root-only env carries the operation id so the same call can recover a crash
between its atomic file replacement and SQLite commit. Runtime persists that id,
recreates and health-checks the frozen application image, and only then calls the
idempotent `commit-rotate`; the old-token grace interval starts at commit time.
A lost response, process kill, or reboot is resumed with the same operation id.
The compatibility `rotate` command performs both phases immediately and therefore
must not replace Runtime's online workflow. Rotation refuses suspended or revoked
applications. Suspension denies all tokens without deleting them; `resume`
reactivates only a suspended app whose installed credential is still valid. A
revoked app can never be resumed.
Revocation invalidates all tokens and removes the exact app env file; it never
deletes OSS objects or a prefix.

## Object API

- `GET /healthz` and `GET /v1/health` — unauthenticated local health.
- `PUT /v1/objects/<relative-key>` — bounded temporary buffering followed by an
  OSS upload, returning key, size, SHA-256, and ETag. The default accepts at
  most 512 MiB per object, runs at most two upload buffers concurrently, and
  preserves at least 5 GiB of ECS disk.
- `GET /v1/objects/<relative-key>` — streamed download.
- `HEAD /v1/objects/<relative-key>` — `Content-Length`, `Content-Type`, `ETag`,
  and `X-Storage-Sha256` when available.
- `DELETE /v1/objects/<relative-key>` — idempotent exact-object delete.

All object operations require `Authorization: Bearer <application-token>`.
Absolute paths, backslashes, empty/dot segments, traversal, and NULs are rejected
before the server-controlled prefix is joined.

## Tests

The tests never contact OSS. They inject an in-memory adapter and verify two-app
prefix isolation, traversal rejection, auth/suspension, token rotation/revocation,
and idempotent provisioning with no plaintext token in SQLite.

```console
python -m pip install -e ".[test]"
python -m pytest
```
