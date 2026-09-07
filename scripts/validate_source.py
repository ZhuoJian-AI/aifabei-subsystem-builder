#!/usr/bin/env python3
"""Fail fast on source-level integration mistakes before deployment."""

from __future__ import annotations

import argparse
import re
from pathlib import Path

TEXT_SUFFIXES = {
    ".html", ".js", ".jsx", ".ts", ".tsx", ".vue", ".svelte", ".py",
    ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
}
TEXT_NAMES = {"Dockerfile", ".env", ".env.example"}
IGNORED_PARTS = {
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    "dist",
    "build",
    "data",
    "docs",
    "documentation",
    "tests",
}
MODEL_PROVIDER_NAMES = (
    "ANTHROPIC",
    "ARK",
    "AZURE_OPENAI",
    "COHERE",
    "DASHSCOPE",
    "DEEPSEEK",
    "GEMINI",
    "GOOGLE_AI",
    "GROQ",
    "LLM",
    "MISTRAL",
    "MODEL",
    "MOONSHOT",
    "OPENAI",
    "QWEN",
    "SILICONFLOW",
    "VOLCENGINE",
    "XAI",
    "ZHIPUAI",
)
WILDCARD_POST_MESSAGE = re.compile(
    r"postMessage\s*\((?:(?!;).){0,8000}?,\s*(['\"])\*\1\s*\)",
    re.DOTALL,
)
PLATFORM_MODEL_CREDENTIAL = re.compile(
    rf"\b(?:{'|'.join(MODEL_PROVIDER_NAMES)})(?:_API)?_(?:KEY|TOKEN|SECRET)\b",
    re.IGNORECASE,
)
PYTHON_MODEL_PROVIDER_SDK = re.compile(
    r"^\s*(?:from|import)\s+(?:"
    r"openai|anthropic|dashscope|google\.(?:generativeai|genai)|"
    r"cohere|mistralai|groq|volcenginesdkarkruntime"
    r")\b",
    re.IGNORECASE | re.MULTILINE,
)
JAVASCRIPT_MODEL_PROVIDER_SDK = re.compile(
    r"(?:from\s+|require\s*\(\s*|import\s*\(\s*)['\"](?:"
    r"openai|@anthropic-ai/sdk|@google/(?:generative-ai|genai)|"
    r"cohere-ai|@mistralai/mistralai|groq-sdk|@alicloud/dashscope-sdk"
    r")[\"']",
    re.IGNORECASE,
)
PYTHON_MODEL_PROVIDER_DEPENDENCY = re.compile(
    r"^\s*(?:openai|anthropic|dashscope|google-generativeai|google-genai|"
    r"cohere|mistralai|groq|volcengine-python-sdk)\s*(?:[<>=!~].*)?$",
    re.IGNORECASE | re.MULTILINE,
)
PACKAGE_MODEL_PROVIDER_DEPENDENCY = re.compile(
    r"['\"](?:openai|@anthropic-ai/sdk|@google/(?:generative-ai|genai)|"
    r"cohere-ai|@mistralai/mistralai|groq-sdk|@alicloud/dashscope-sdk)['\"]\s*:",
    re.IGNORECASE,
)
PYPROJECT_MODEL_PROVIDER_DEPENDENCY = re.compile(
    r"['\"](?:openai|anthropic|dashscope|google-generativeai|google-genai|"
    r"cohere|mistralai|groq|volcengine-python-sdk)(?:[<>=!~][^'\"]*)?['\"]",
    re.IGNORECASE,
)
DIRECT_MODEL_PROVIDER_URL = re.compile(
    r"https?://(?:"
    r"api\.openai\.com|api\.anthropic\.com|api\.deepseek\.com|"
    r"(?:[a-z0-9-]+\.)*dashscope\.aliyuncs\.com|"
    r"[a-z0-9.-]*maas\.aliyuncs\.com|"
    r"generativelanguage\.googleapis\.com|api\.moonshot\.cn|"
    r"api\.siliconflow\.cn|token-plan-cn\.xiaomimimo\.com|"
    r"api\.mistral\.ai|api\.groq\.com|api\.cohere\.ai|"
    r"ark\.[a-z0-9-]+\.volces\.com"
    r")(?=[:/]|$)",
    re.IGNORECASE,
)
LEGACY_SHARED_INTEGRATION_CREDENTIAL = re.compile(
    r"\bZHUOJIAN_(?:INTEGRATION_SECRET|SHARED_(?:INTEGRATION_)?SECRET)\b",
    re.IGNORECASE,
)
REQUIRED_INTEGRATION_MARKERS = {
    "ZHUOJIAN_MANIFEST_ACCESS_TOKEN",
    "ZHUOJIAN_SSO_EXCHANGE_TOKEN",
    "ZHUOJIAN_ACTION_SIGNING_SECRET",
    "ZHUOJIAN_EVENT_SIGNING_SECRET",
    "/api/v1/subsystem-sso/exchange",
    "launch_nonce",
}
EMBEDDED_MODE_MARKER = "data-zhuojian-embedded"
NESTED_IFRAME = re.compile(r"<iframe\b|createElement\s*\(\s*(['\"])iframe\1", re.IGNORECASE)
FRAME_ANCESTOR_DIRECTIVE = re.compile(
    r"frame-ancestors(?P<sources>[^;\r\n]{0,500})",
    re.IGNORECASE,
)


def uses_model_provider_sdk(path: Path, text: str) -> bool:
    if PYTHON_MODEL_PROVIDER_SDK.search(text) or JAVASCRIPT_MODEL_PROVIDER_SDK.search(text):
        return True
    name = path.name.lower()
    if name.startswith("requirements") and name.endswith(".txt"):
        return bool(PYTHON_MODEL_PROVIDER_DEPENDENCY.search(text))
    if name in {"package.json", "package-lock.json", "npm-shrinkwrap.json"}:
        return bool(PACKAGE_MODEL_PROVIDER_DEPENDENCY.search(text))
    if name == "pyproject.toml":
        return bool(PYPROJECT_MODEL_PROVIDER_DEPENDENCY.search(text))
    return False


def source_files(root: Path):
    for path in root.rglob("*"):
        if not path.is_file() or (
            path.suffix.lower() not in TEXT_SUFFIXES
            and path.name not in TEXT_NAMES
            and not path.name.startswith(".env.")
            and not (
                path.name.lower().startswith("requirements")
                and path.suffix.lower() == ".txt"
            )
        ):
            continue
        if (
            any(part.lower() in IGNORED_PARTS for part in path.parts)
            or path.stat().st_size > 5_000_000
        ):
            continue
        yield path


def main() -> int:
    parser = argparse.ArgumentParser(description="校验灼见原生模块的前端 Bridge 安全约束")
    parser.add_argument("--path", required=True, help="模块项目根目录")
    parser.add_argument(
        "--requires-file-storage",
        action="store_true",
        help="模块存在持久文件时启用；校验可迁移存储标记并拒绝 OSS AccessKey",
    )
    parser.add_argument(
        "--requires-object-storage",
        action="store_true",
        help="环境已明确启用 OSS 时追加；校验企业文件网关标记",
    )
    args = parser.parse_args()
    root = Path(args.path).expanduser().resolve()
    if not root.is_dir():
        parser.error(f"目录不存在：{root}")

    context_found = False
    bridge_ready_found = False
    bridge_launch_binding_found = False
    bridge_refresh_found = False
    bridge_refresh_result_found = False
    bridge_refresh_binding_found = False
    bridge_refresh_deferred_found = False
    bridge_refresh_reloads_page = False
    embedded_mode_found = False
    nested_iframe_found = False
    unsafe_frame_ancestor_files: set[Path] = set()
    page_scope_tokens: set[str] = set()
    integration_markers: set[str] = set()
    storage_markers: set[str] = set()
    failures: list[str] = []
    for path in source_files(root):
        text = path.read_text(encoding="utf-8", errors="replace")
        context_found = context_found or "zhuojian:context" in text
        bridge_ready_found = bridge_ready_found or "zhuojian:ready" in text
        bridge_launch_binding_found = bridge_launch_binding_found or (
            "zhuojian:context" in text and "launch_nonce" in text
        )
        if re.search(r"zhuojian:refresh(?!-result)", text):
            bridge_refresh_found = True
            bridge_refresh_result_found = bridge_refresh_result_found or "zhuojian:refresh-result" in text
            bridge_refresh_binding_found = bridge_refresh_binding_found or all(
                marker in text
                for marker in (
                    "launch_nonce", "module_key", "page_key", "request_id",
                    "event.origin", "event.source",
                )
            )
            bridge_refresh_deferred_found = bridge_refresh_deferred_found or "deferred" in text
            for match in re.finditer(r"zhuojian:refresh(?:-result)?", text):
                region = text[max(0, match.start() - 4_000):match.end() + 8_000]
                if re.search(r"(?:window\.)?location\.reload\s*\(", region):
                    bridge_refresh_reloads_page = True
        embedded_mode_found = embedded_mode_found or EMBEDDED_MODE_MARKER in text
        nested_iframe_found = nested_iframe_found or bool(NESTED_IFRAME.search(text))
        if any(
            "'self'" not in match.group("sources")
            for match in FRAME_ANCESTOR_DIRECTIVE.finditer(text)
        ):
            unsafe_frame_ancestor_files.add(path.relative_to(root))
        page_scope_tokens.update(
            token for token in (
                "pageKeys", "actionKeys", "pageAccess", "roleIds", "effectiveDataScope"
            ) if token in text
        )
        integration_markers.update(
            token for token in REQUIRED_INTEGRATION_MARKERS if token in text
        )
        storage_markers.update(token for token in (
            "FILE_STORAGE_DRIVER",
            "FILE_STORAGE_ROOT",
            "FILE_STORAGE_GATEWAY_URL",
            "FILE_STORAGE_TOKEN",
            "FILE_STORAGE_UPLOAD_LOCK_FILE",
            "Content-Length is required for file uploads",
            "deletion_state='uploading'",
            "recover_pending_deletions_once",
        ) if token in text)
        if PLATFORM_MODEL_CREDENTIAL.search(text):
            failures.append(
                f"{path.relative_to(root)}: 平台模型供应商凭证只能配置在灼见 SaaS 底座"
            )
        if uses_model_provider_sdk(path, text):
            failures.append(
                f"{path.relative_to(root)}: 业务系统禁止安装或直接调用模型供应商 SDK"
            )
        if DIRECT_MODEL_PROVIDER_URL.search(text):
            failures.append(
                f"{path.relative_to(root)}: 业务系统禁止直连模型供应商 URL，模型调用必须经过灼见 SaaS"
            )
        if (args.requires_file_storage or args.requires_object_storage) and re.search(
            r"(?:ALIYUN|OSS)_(?:ACCESS|SECRET)[A-Z_]*KEY|AccessKeySecret|accessKeyId",
            text,
            re.IGNORECASE,
        ):
            failures.append(f"{path.relative_to(root)}: 业务源码禁止使用 OSS AccessKey")
        if WILDCARD_POST_MESSAGE.search(text):
            failures.append(f"{path.relative_to(root)}: postMessage targetOrigin 禁止使用 '*' ")
        if LEGACY_SHARED_INTEGRATION_CREDENTIAL.search(text):
            failures.append(
                f"{path.relative_to(root)}: v2.5 禁止用一个旧接入密钥承担多种用途"
            )

    if not context_found:
        failures.append("未找到 zhuojian:context 页面上下文 Bridge")
    if not bridge_ready_found:
        failures.append("未找到绑定本次 SSO 启动的 zhuojian:ready Bridge 就绪消息")
    if not bridge_launch_binding_found:
        failures.append("zhuojian:context 未携带本次启动的 launch_nonce")
    if not bridge_refresh_found:
        failures.append("未找到 zhuojian:refresh 当前模块静默刷新处理")
    if not bridge_refresh_result_found:
        failures.append("未找到 zhuojian:refresh-result 静默刷新结果")
    if not bridge_refresh_binding_found:
        failures.append("静默刷新未同时校验来源、当前窗口、模块、页面、请求号和 launch_nonce")
    if not bridge_refresh_deferred_found:
        failures.append("静默刷新未在存在未保存编辑时返回 deferred")
    if bridge_refresh_reloads_page:
        failures.append("zhuojian:refresh 禁止调用 location.reload()，必须只刷新当前模块数据")
    if not embedded_mode_found:
        failures.append(
            "未实现 iframe 原生嵌入模式：页面需要在嵌入时隐藏自身系统级导航"
        )
    if nested_iframe_found and unsafe_frame_ancestor_files:
        failures.append(
            "系统包含内层 iframe，但以下 frame-ancestors 未包含 'self'，"
            "会阻断同源业务页面："
            + ", ".join(sorted(str(path) for path in unsafe_frame_ancestor_files))
        )
    missing_scope = {
        "pageKeys", "actionKeys", "pageAccess", "roleIds", "effectiveDataScope"
    } - page_scope_tokens
    if missing_scope:
        failures.append(
            "未实现 v2.5 SSO 页面/操作/角色/数据范围："
            + ", ".join(sorted(missing_scope))
        )
    missing_integration = REQUIRED_INTEGRATION_MARKERS - integration_markers
    if missing_integration:
        failures.append(
            "未实现 v2.5 分用途凭证和一次性 SSO 换码："
            + ", ".join(sorted(missing_integration))
        )
    if args.requires_file_storage or args.requires_object_storage:
        missing_storage = {
            "FILE_STORAGE_DRIVER",
            "FILE_STORAGE_ROOT",
            "FILE_STORAGE_UPLOAD_LOCK_FILE",
            "Content-Length is required for file uploads",
            "deletion_state='uploading'",
            "recover_pending_deletions_once",
        } - storage_markers
        if missing_storage:
            failures.append(
                "持久文件必须使用可迁移存储适配层，未找到："
                + ", ".join(sorted(missing_storage))
            )
    if args.requires_object_storage:
        missing_storage = {
            "FILE_STORAGE_GATEWAY_URL", "FILE_STORAGE_TOKEN"
        } - storage_markers
        if missing_storage:
            failures.append(
                "OSS 模式必须使用 Alphabet 企业文件网关，未找到："
                + ", ".join(sorted(missing_storage))
            )
    if failures:
        print("SOURCE VALIDATION FAILED")
        for failure in failures:
            print(f"- {failure}")
        return 1
    print(
        "SOURCE VALIDATION PASS: platform model credentials are absent; "
        "embedded shell, bridge origin and v2.5 SSO/page/action/resource-scoped data access are present"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
