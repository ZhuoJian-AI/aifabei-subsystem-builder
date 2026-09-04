from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .config import GatewaySettings, OssCredentials, load_oss_credentials
from .registry import CredentialRegistry, RegistryError


_REGION = re.compile(r"^[a-z0-9][a-z0-9-]+$")


def build_parser() -> argparse.ArgumentParser:
    defaults = GatewaySettings.from_environment()
    parser = argparse.ArgumentParser(
        prog="zhuojian-storage-gateway-admin",
        description="Root-only local management for application-scoped storage credentials",
    )
    parser.add_argument("--db-path", type=Path, default=defaults.db_path)
    parser.add_argument("--apps-env-dir", type=Path, default=defaults.apps_env_dir)
    parser.add_argument("--gateway-url", default=defaults.internal_url)
    subparsers = parser.add_subparsers(dest="command", required=True)

    ensure = subparsers.add_parser("ensure-app", help="idempotently provision an app scope")
    ensure.add_argument("--application-slug", required=True)

    rotate = subparsers.add_parser("rotate", help="rotate an app credential")
    rotate.add_argument("--application-slug", required=True)
    rotate.add_argument("--grace-seconds", type=int, default=300)

    suspend = subparsers.add_parser("suspend", help="temporarily deny an app credential")
    suspend.add_argument("--application-slug", required=True)

    revoke = subparsers.add_parser("revoke", help="permanently revoke an app credential")
    revoke.add_argument("--application-slug", required=True)

    probe = subparsers.add_parser(
        "probe",
        help="verify the configured OSS scope and two-application isolation end to end",
    )
    probe.add_argument("--expected-bucket", required=True)
    probe.add_argument("--expected-region", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    _require_root()
    parser = build_parser()
    args = parser.parse_args(argv)
    registry = CredentialRegistry(args.db_path)

    try:
        if args.command == "ensure-app":
            result = registry.ensure_app(
                args.application_slug, args.apps_env_dir, args.gateway_url
            )
            _print_safe_result(
                action="ensure-app",
                slug=result.slug,
                status=result.status,
                env_path=result.env_path,
                reused=result.reused,
            )
        elif args.command == "rotate":
            result = registry.rotate_app(
                args.application_slug,
                args.apps_env_dir,
                args.gateway_url,
                grace_seconds=args.grace_seconds,
            )
            _print_safe_result(
                action="rotate",
                slug=result.slug,
                status=result.status,
                env_path=result.env_path,
                reused=False,
            )
        elif args.command == "suspend":
            registry.suspend_app(args.application_slug)
            _print_safe_result(
                action="suspend",
                slug=args.application_slug,
                status="suspended",
            )
        elif args.command == "revoke":
            registry.revoke_app(args.application_slug, args.apps_env_dir)
            _print_safe_result(
                action="revoke",
                slug=args.application_slug,
                status="revoked",
            )
        elif args.command == "probe":
            result = probe_gateway(
                registry=registry,
                settings=GatewaySettings.from_environment(),
                apps_env_dir=args.apps_env_dir,
                gateway_url=args.gateway_url,
                expected_bucket=args.expected_bucket,
                expected_region=args.expected_region,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        else:  # guarded by argparse
            parser.error("unknown command")
    except (RegistryError, GatewayProbeError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), file=sys.stderr)
        return 2
    return 0


class GatewayProbeError(RuntimeError):
    """A safe, non-secret failure from the administrator acceptance probe."""


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def probe_gateway(
    *,
    registry: CredentialRegistry,
    settings: GatewaySettings,
    apps_env_dir: Path,
    gateway_url: str,
    expected_bucket: str,
    expected_region: str,
) -> dict[str, object]:
    """Exercise real OSS through two temporary app identities without leaking tokens."""

    bucket = expected_bucket.strip()
    region = expected_region.strip()
    if not bucket or not _REGION.fullmatch(region):
        raise GatewayProbeError("expected OSS bucket or region is invalid")
    credentials = load_oss_credentials(settings.oss_secrets_file)
    endpoint_host = (urlsplit(credentials.endpoint).hostname or "").lower()
    endpoint_region = region if region.startswith("oss-") else f"oss-{region}"
    expected_hosts = {
        f"{endpoint_region}.aliyuncs.com",
        f"{endpoint_region}-internal.aliyuncs.com",
    }
    if credentials.bucket != bucket or endpoint_host not in expected_hosts:
        raise GatewayProbeError("OSS secret scope does not match the requested bucket and region")

    run_id = secrets.token_hex(6)
    _assert_outside_prefix_denied(credentials, run_id)
    slugs = (f"storage-probe-a-{run_id}", f"storage-probe-b-{run_id}")
    relative_key = f"acceptance/{run_id}.bin"
    payload_a = f"alphabet-storage-probe-a:{run_id}".encode()
    payload_b = f"alphabet-storage-probe-b:{run_id}".encode()
    provisioned: list[str] = []
    tokens: dict[str, str] = {}
    cleanup_failed = False
    opener = build_opener(ProxyHandler({}), _RejectRedirects())

    try:
        for slug in slugs:
            result = registry.ensure_app(slug, apps_env_dir, gateway_url)
            provisioned.append(slug)
            tokens[slug] = _read_generated_token(result.env_path)

        _request_object(opener, gateway_url, tokens[slugs[0]], "PUT", relative_key, payload_a, 201)
        received_a = _request_object(
            opener, gateway_url, tokens[slugs[0]], "GET", relative_key, None, 200
        )
        if received_a != payload_a:
            raise GatewayProbeError("OSS round-trip payload mismatch")

        _request_object(opener, gateway_url, tokens[slugs[1]], "GET", relative_key, None, 404)
        _request_object(opener, gateway_url, tokens[slugs[1]], "PUT", relative_key, payload_b, 201)
        received_b = _request_object(
            opener, gateway_url, tokens[slugs[1]], "GET", relative_key, None, 200
        )
        received_a_again = _request_object(
            opener, gateway_url, tokens[slugs[0]], "GET", relative_key, None, 200
        )
        if received_b != payload_b or received_a_again != payload_a:
            raise GatewayProbeError("application storage prefixes are not isolated")

        for slug in slugs:
            _request_object(opener, gateway_url, tokens[slug], "DELETE", relative_key, None, 204)
            _request_object(opener, gateway_url, tokens[slug], "GET", relative_key, None, 404)
    finally:
        for slug in provisioned:
            token = tokens.get(slug)
            if token:
                try:
                    _request_object(
                        opener, gateway_url, token, "DELETE", relative_key, None, {204, 404}
                    )
                except GatewayProbeError:
                    pass
            try:
                registry.revoke_app(slug, apps_env_dir)
            except RegistryError:
                cleanup_failed = True

    if cleanup_failed:
        raise GatewayProbeError("temporary application credentials could not be revoked")

    return {
        "ok": True,
        "action": "probe",
        "bucket": bucket,
        "region": region,
        "checks": {
            "put": True,
            "get": True,
            "delete": True,
            "twoApplicationIsolation": True,
            "temporaryCredentialsRevoked": True,
            "outsidePrefixDenied": True,
        },
    }


def _assert_outside_prefix_denied(credentials: OssCredentials, run_id: str) -> None:
    """Verify the RAM identity cannot list or read outside ``apps/*``."""

    try:
        import oss2

        if credentials.security_token:
            auth = oss2.StsAuth(
                credentials.access_key_id,
                credentials.access_key_secret,
                credentials.security_token,
            )
        else:
            auth = oss2.Auth(
                credentials.access_key_id,
                credentials.access_key_secret,
            )
        bucket = oss2.Bucket(auth, credentials.endpoint, credentials.bucket)
        outside_prefix = f"zhuojian-policy-probe/{run_id}/"
        checks = (
            lambda: bucket.list_objects_v2(prefix=outside_prefix, max_keys=1),
            lambda: bucket.head_object(f"{outside_prefix}missing.bin"),
        )
        for operation in checks:
            try:
                operation()
            except Exception as exc:
                if getattr(exc, "status", None) == 403:
                    continue
                raise GatewayProbeError(
                    "OSS policy denial probe could not confirm the outside-prefix boundary"
                ) from exc
            raise GatewayProbeError("OSS credential is not restricted to the apps prefix")
    except GatewayProbeError:
        raise
    except Exception as exc:
        raise GatewayProbeError("OSS policy denial probe could not run") from exc


def _read_generated_token(path: Path) -> str:
    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if "=" in raw_line:
            name, value = raw_line.split("=", 1)
            values[name] = value
    token = values.get("FILE_STORAGE_TOKEN", "")
    if not token or any(character.isspace() for character in token):
        raise GatewayProbeError("temporary application credential was not generated")
    return token


def _request_object(
    opener,
    gateway_url: str,
    token: str,
    method: str,
    key: str,
    body: bytes | None,
    expected_status: int | set[int],
) -> bytes:
    expected = {expected_status} if isinstance(expected_status, int) else expected_status
    encoded_key = "/".join(quote(part, safe="-._~") for part in key.split("/"))
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers.update(
            {
                "Content-Type": "application/octet-stream",
                "Content-Length": str(len(body)),
                "X-Storage-Sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    request = Request(
        f"{gateway_url.rstrip('/')}/v1/objects/{encoded_key}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with opener.open(request, timeout=30) as response:
            status_code = response.status
            payload = response.read(1024 * 1024)
    except HTTPError as exc:
        status_code = exc.code
        exc.close()
        payload = b""
    except (URLError, TimeoutError, OSError) as exc:
        raise GatewayProbeError("storage gateway acceptance request failed") from exc
    if status_code not in expected:
        raise GatewayProbeError(
            f"storage gateway returned HTTP {status_code} for {method}; expected {sorted(expected)}"
        )
    return payload


def _require_root() -> None:
    if os.name == "posix" and os.geteuid() != 0:
        raise SystemExit("zhuojian-storage-gateway-admin must run as root")


def _print_safe_result(
    *,
    action: str,
    slug: str,
    status: str,
    env_path: Path | None = None,
    reused: bool | None = None,
) -> None:
    result: dict[str, object] = {
        "ok": True,
        "action": action,
        "applicationSlug": slug,
        "status": status,
    }
    if env_path is not None:
        result["envPath"] = str(env_path)
    if reused is not None:
        result["reused"] = reused
    # By construction this payload can never contain a plaintext credential.
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    raise SystemExit(main())
