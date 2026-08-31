#!/usr/bin/env python3
"""Read-only inventory for an existing or new Aifabei module repository."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def contains(root: Path, names: list[str], markers: tuple[str, ...]) -> dict[str, bool]:
    text = ""
    for name in names:
        path = root / name
        if path.is_file() and path.stat().st_size <= 5_000_000:
            text += "\n" + path.read_text(encoding="utf-8", errors="replace")
    scanned = 0
    ignored = {".git", ".venv", "node_modules", "dist", "build", "data"}
    for path in root.rglob("*"):
        if scanned >= 500 or not path.is_file() or any(part in ignored for part in path.parts):
            continue
        if path.suffix.lower() not in {".py", ".js", ".ts", ".tsx", ".html", ".md", ".yml", ".yaml"}:
            continue
        if path.stat().st_size > 5_000_000:
            continue
        text += "\n" + path.read_text(encoding="utf-8", errors="replace")
        scanned += 1
    return {marker: marker in text for marker in markers}


def main() -> int:
    parser = argparse.ArgumentParser(description="只读检查爱法贝业务模块系统")
    parser.add_argument("--path", required=True, help="仓库根目录")
    parser.add_argument("--json", action="store_true", help="输出 JSON")
    args = parser.parse_args()
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"目录不存在：{root}")

    files = {
        "git": (root / ".git").exists(),
        "agents": (root / "AGENTS.md").is_file() or (root / "AGENTS.override.md").is_file(),
        "readme": any((root / name).is_file() for name in ("README.md", "README.txt")),
        "manifest": (root / "deploy" / "project.yaml").is_file(),
        "compose": any((root / name).is_file() for name in ("docker-compose.yml", "docker-compose.yaml", "compose.yml", "compose.yaml")),
        "dockerfile": (root / "Dockerfile").is_file(),
    }
    markers = contains(
        root,
        ["index.html", "parser_service.py", "app.py", "server.py", "README.md", "docker-compose.yml"],
        (
            "/health",
            "zhuojian:context",
            "/api/integration/manifest",
            "/api/integration/actions",
            "/api/integration/events",
            "/api/integration/sso",
        ),
    )
    manifest_path = root / "subsystem.json"
    manifest: dict | None = None
    manifest_error: str | None = None
    if manifest_path.is_file():
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("subsystem.json 顶层必须是对象")
            manifest = payload
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            manifest_error = str(exc)

    modules = []
    if manifest is not None:
        for item in manifest.get("modules") or []:
            if not isinstance(item, dict):
                continue
            modules.append({
                "moduleKey": item.get("moduleKey"),
                "name": item.get("name"),
                "route": item.get("route"),
                "departments": [
                    department.get("key") for department in item.get("departments") or []
                    if isinstance(department, dict) and department.get("key")
                ],
                "pageKeys": [
                    page.get("pageKey") for page in item.get("pages") or []
                    if isinstance(page, dict) and page.get("pageKey")
                ],
                "actionKeys": [
                    action.get("actionKey") for action in item.get("actions") or []
                    if isinstance(action, dict) and action.get("actionKey")
                ],
            })
    report = {
        "root": str(root),
        "files": files,
        "contract": markers,
        "manifest": {
            "path": str(manifest_path) if manifest_path.is_file() else None,
            "error": manifest_error,
            "enterpriseKey": (manifest or {}).get("enterprise", {}).get("key")
            if isinstance((manifest or {}).get("enterprise"), dict) else None,
            "applicationSlug": (manifest or {}).get("applicationSlug"),
            "applicationName": (manifest or {}).get("applicationName"),
            "contractRevision": (manifest or {}).get("contractRevision"),
            "moduleCount": len(modules),
            "modules": modules,
        },
        "recommendedMode": (
            "extend-existing-system" if manifest is not None and modules
            else "inspect-manually" if manifest_error
            else "new-system-or-legacy"
        ),
    }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"项目：{root}")
        for label, ok in files.items():
            print(f"- {'已有' if ok else '缺少'} {label}")
        for marker, ok in markers.items():
            print(f"- {'已有' if ok else '缺少'} 接入标记 {marker}")
        if manifest_error:
            print(f"- Manifest 读取失败：{manifest_error}")
        elif manifest is not None:
            print(f"- applicationSlug：{manifest.get('applicationSlug') or '缺少'}")
            print(f"- 子模块：{len(modules)} 个")
            for module in modules:
                print(
                    f"  - {module.get('moduleKey')}: "
                    f"页面 {len(module['pageKeys'])}，Action {len(module['actionKeys'])}"
                )
        print("本脚本只读，不读取或输出任何密钥值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
