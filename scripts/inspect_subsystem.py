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
        ("/health", "zhuojian:context", "/api/integration/manifest", "/api/integration/actions", "/api/integration/events"),
    )
    report = {"root": str(root), "files": files, "contract": markers}
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"项目：{root}")
        for label, ok in files.items():
            print(f"- {'已有' if ok else '缺少'} {label}")
        for marker, ok in markers.items():
            print(f"- {'已有' if ok else '缺少'} 接入标记 {marker}")
        print("本脚本只读，不读取或输出任何密钥值。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
