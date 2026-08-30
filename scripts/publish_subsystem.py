#!/usr/bin/env python3
"""Provision the tenant repository centrally and push without persisting GitHub credentials."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


def run_git(root: Path, *args: str, env: dict[str, str] | None = None) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode != 0:
        detail = process.stderr.strip().splitlines()[-1] if process.stderr.strip() else "git command failed"
        raise RuntimeError(detail)
    return process.stdout.strip()


def normalized_remote(value: str) -> str:
    return value.strip().removesuffix(".git").rstrip("/").lower()


def provision(platform_url: str, publish_key: str, module_slug: str, module_name: str) -> dict:
    endpoint = urllib.parse.urljoin(platform_url.rstrip("/") + "/", "api/v1/module-publisher/repositories")
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("发布服务必须使用 HTTPS")
    request = urllib.request.Request(
        endpoint,
        data=json.dumps({"module_slug": module_slug, "module_name": module_name}).encode("utf-8"),
        headers={
            "content-type": "application/json",
            "x-api-key": publish_key,
            "user-agent": "ZhuoJian-Subsystem-Publisher/1.0",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8")).get("detail")
        except (UnicodeDecodeError, json.JSONDecodeError):
            detail = None
        raise RuntimeError(str(detail or f"发布服务返回 HTTP {exc.code}")) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError("无法连接灼见发布服务") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description="创建/查找企业私有仓库并安全推送当前提交")
    parser.add_argument("--path", required=True, help="模块项目根目录")
    parser.add_argument("--platform-url", default=os.environ.get("ZHUOJIAN_PLATFORM_URL", ""))
    parser.add_argument("--remote", default="origin")
    args = parser.parse_args()

    root = Path(args.path).expanduser().resolve()
    config_path = root / "subsystem.json"
    if not config_path.is_file() or not (root / ".git").exists():
        parser.error("项目必须包含 subsystem.json 和本地 Git 仓库")
    if not args.platform_url.strip():
        parser.error("缺少 ZHUOJIAN_PLATFORM_URL")
    publish_key = os.environ.get("ZHUOJIAN_PUBLISH_KEY", "").strip()
    if not publish_key:
        parser.error("缺少 ZHUOJIAN_PUBLISH_KEY；请先完成公司环境初始化")

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
    print(json.dumps({
        "status": "published",
        "repositoryName": repository_name,
        "cloneUrl": clone_url,
        "created": bool(result.get("created")),
        "sourceCommit": run_git(root, "rev-parse", "HEAD"),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"发布失败：{exc}", file=sys.stderr)
        raise SystemExit(1) from exc
