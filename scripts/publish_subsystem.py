#!/usr/bin/env python3
"""Provision the tenant repository centrally and push without persisting GitHub credentials."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_PUBLISH_KEY_FILE = Path("/etc/zhuojian/publisher.key")


def run_git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip().splitlines()[-1] if process.stderr.strip() else "git command failed"
        raise RuntimeError(detail)
    return process.stdout.strip()


def normalized_remote(value: str) -> str:
    return value.strip().removesuffix(".git").rstrip("/").lower()


def load_publish_key() -> str:
    direct = os.environ.get("ZHUOJIAN_PUBLISH_KEY", "").strip()
    if direct:
        return direct
    key_path = Path(
        os.environ.get("ZHUOJIAN_PUBLISH_KEY_FILE", str(DEFAULT_PUBLISH_KEY_FILE))
    ).expanduser()
    try:
        return key_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except OSError as exc:
        raise RuntimeError(f"无法读取公司发布凭证文件：{key_path}") from exc


def platform_request(
    platform_url: str,
    publish_key: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout: int = 30,
) -> dict:
    endpoint = urllib.parse.urljoin(platform_url.rstrip("/") + "/", path.lstrip("/"))
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("发布服务必须使用 HTTPS")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps(payload).encode("utf-8") if payload is not None else None,
        headers={
            "content-type": "application/json",
            "x-api-key": publish_key,
            "user-agent": "ZhuoJian-Subsystem-Publisher/1.0",
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = None
        raise RuntimeError(str(detail or f"发布服务返回 HTTP {exc.code}")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("无法连接灼见发布服务") from exc


def provision(platform_url: str, publish_key: str, module_slug: str, module_name: str) -> dict:
    return platform_request(
        platform_url,
        publish_key,
        "api/v1/module-publisher/repositories",
        method="POST",
        payload={"module_slug": module_slug, "module_name": module_name},
    )


def request_deployment(
    platform_url: str,
    publish_key: str,
    *,
    module_slug: str,
    module_name: str,
    repository_name: str,
    source_commit: str,
    runtime_key: str | None,
) -> dict:
    return platform_request(
        platform_url,
        publish_key,
        "api/v1/module-publisher/deployments",
        method="POST",
        payload={
            "module_slug": module_slug,
            "module_name": module_name,
            "repository_name": repository_name,
            "source_commit": source_commit,
            "runtime_key": runtime_key,
        },
        timeout=60,
    )


def wait_for_deployment(
    platform_url: str,
    publish_key: str,
    module_slug: str,
    timeout_seconds: int,
) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_poll_error = ""
    while time.monotonic() < deadline:
        try:
            result = platform_request(
                platform_url,
                publish_key,
                f"api/v1/module-publisher/deployments/{urllib.parse.quote(module_slug, safe='')}",
                timeout=45,
            )
            last_poll_error = ""
        except RuntimeError as exc:
            last_poll_error = str(exc)
            time.sleep(5)
            continue
        status = str(result.get("status") or "")
        if status == "healthy":
            return result
        if status in {"failed", "rolled_back", "rollback_failed"}:
            detail = str(result.get("detail") or "部署失败")
            next_action = str(result.get("next_action") or "")
            stage = str(result.get("failure_stage") or "deploy")
            raise RuntimeError(
                f"部署未通过（{stage}）：{detail}"
                + (f"；建议：{next_action}" if next_action else "")
            )
        time.sleep(5)
    suffix = f"；最后一次查询错误：{last_poll_error}" if last_poll_error else ""
    raise RuntimeError(
        "部署仍在进行但等待超时；稍后重新运行发布命令可继续查询同一发布记录" + suffix
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="安全推送企业私有仓库，并由灼见部署到企业 Coolify 后自动登记")
    parser.add_argument("--path", required=True, help="模块项目根目录")
    parser.add_argument("--platform-url", default=os.environ.get("ZHUOJIAN_PLATFORM_URL", ""))
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--wait-timeout", type=int, default=900, help="等待部署、健康检查和平台登记的秒数")
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    config_path = root / "subsystem.json"
    if not config_path.is_file() or not (root / ".git").exists():
        parser.error("项目必须包含 subsystem.json 和本地 Git 仓库")
    if not args.platform_url.strip():
        parser.error("缺少 ZHUOJIAN_PLATFORM_URL")
    publish_key = load_publish_key()
    if not publish_key:
        parser.error(
            "缺少公司发布凭证；请先完成公司环境初始化（环境变量 ZHUOJIAN_PUBLISH_KEY "
            "或 /etc/zhuojian/publisher.key）"
        )

    config = json.loads(config_path.read_text(encoding="utf-8"))
    module_slug = str(config.get("applicationSlug") or "").strip()
    module_name = str(config.get("applicationName") or "").strip()
    if not module_slug or not module_name:
        parser.error("subsystem.json 缺少 applicationSlug 或 applicationName")
    run_git(root, "rev-parse", "--verify", "HEAD")
    if run_git(root, "status", "--porcelain"):
        parser.error("工作树不是干净状态；请先检查并提交本次模块修改")

    result = provision(args.platform_url, publish_key, module_slug, module_name)
    clone_url = str(result.get("clone_url") or "")
    access_token = str(result.get("access_token") or "")
    repository_name = str(result.get("repository_name") or "")
    if not clone_url.startswith("https://github.com/") or not access_token or not repository_name:
        raise RuntimeError("发布服务响应不完整")

    remotes = run_git(root, "remote").splitlines()
    if args.remote in remotes:
        current = run_git(root, "remote", "get-url", args.remote)
        if normalized_remote(current) != normalized_remote(clone_url):
            raise RuntimeError(f"远程 {args.remote} 已指向其他仓库，拒绝覆盖")
    else:
        run_git(root, "remote", "add", args.remote, clone_url)

    # Pass the one-hour repository token through Git's process environment.
    # It is never written to .git/config, command arguments, stdout, or disk.
    credentials = base64.b64encode(f"x-access-token:{access_token}".encode()).decode("ascii")
    git_env = os.environ.copy()
    git_env.update({
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {credentials}",
        "GIT_TERMINAL_PROMPT": "0",
    })
    run_git(root, "push", "-u", args.remote, "HEAD:main", env=git_env)
    source_commit = run_git(root, "rev-parse", "HEAD")
    deployment = request_deployment(
        args.platform_url,
        publish_key,
        module_slug=module_slug,
        module_name=module_name,
        repository_name=repository_name,
        source_commit=source_commit,
        runtime_key=os.environ.get("ZHUOJIAN_RUNTIME_KEY", "").strip() or None,
    )
    if str(deployment.get("status") or "") != "healthy":
        deployment = wait_for_deployment(
            args.platform_url,
            publish_key,
            module_slug,
            max(30, args.wait_timeout),
        )
    print(json.dumps({
        "status": "healthy",
        "repositoryName": repository_name,
        "cloneUrl": clone_url,
        "created": bool(result.get("created")),
        "sourceCommit": source_commit,
        "entryUrl": deployment.get("entry_url"),
        "coolifyApplicationUuid": deployment.get("coolify_application_uuid"),
        "deploymentUuid": deployment.get("deployment_uuid"),
        "platformApplicationId": deployment.get("application_id"),
        "platformRegistration": "synced",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"发布失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
