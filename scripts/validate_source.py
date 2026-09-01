#!/usr/bin/env python3
"""Fail fast on source-level integration mistakes before deployment."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


TEXT_SUFFIXES = {".html", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".py"}
IGNORED_PARTS = {".git", ".venv", "node_modules", "dist", "build", "data"}
WILDCARD_POST_MESSAGE = re.compile(
    r"postMessage\s*\((?:(?!;).){0,8000}?,\s*(['\"])\*\1\s*\)",
    re.DOTALL,
)


def source_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        if any(part in IGNORED_PARTS for part in path.parts) or path.stat().st_size > 5_000_000:
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description="校验灼见原生模块的前端 Bridge 安全约束")
    parser.add_argument("--path", required=True, help="模块项目根目录")
    args = parser.parse_args()
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"目录不存在：{root}")

    context_found = False
    page_scope_tokens: set[str] = set()
    failures: list[str] = []
    for path in source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        context_found = context_found or "zhuojian:context" in text
        page_scope_tokens.update(
            token for token in ("pageKeys", "actionKeys", "pageAccess") if token in text
        )
        if WILDCARD_POST_MESSAGE.search(text):
            failures.append(f"{path.relative_to(root)}: postMessage targetOrigin 禁止使用 '*' ")

    if not context_found:
        failures.append("未找到 zhuojian:context 页面上下文 Bridge")
    missing_scope = {"pageKeys", "actionKeys", "pageAccess"} - page_scope_tokens
    if missing_scope:
        failures.append(
            "未实现 v2.4 SSO 页面/操作 allowlist：" + ", ".join(sorted(missing_scope))
        )
    if failures:
        print("SOURCE VALIDATION FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print("SOURCE VALIDATION PASS: bridge origin and v2.4 SSO page/action scope are present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
