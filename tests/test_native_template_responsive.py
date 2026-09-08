from __future__ import annotations

import shutil
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_HTML = ROOT / "assets" / "native-subsystem-template" / "static" / "index.html"
VIEWPORTS = (
    (320, 568),
    (390, 844),
    (844, 390),
    (768, 1024),
    (1024, 768),
    (1280, 720),
    (1440, 900),
    (1920, 1080),
)


def browser_executable() -> str | None:
    candidates = (
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
        Path("C:/Program Files (x86)/Google/Chrome/Application/chrome.exe"),
        Path("/usr/bin/google-chrome"),
        Path("/usr/bin/chromium"),
        Path("/usr/bin/chromium-browser"),
    )
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return shutil.which("google-chrome") or shutil.which("chromium")


def assert_viewport(page, width: int, height: int) -> None:
    page.set_viewport_size({"width": width, "height": height})
    page.wait_for_timeout(50)
    dimensions = page.evaluate(
        """() => ({
          client: document.documentElement.clientWidth,
          html: document.documentElement.scrollWidth,
          body: document.body.scrollWidth,
          buttons: [...document.querySelectorAll('button')].filter((button) => button.getClientRects().length).map((button) => {
            const box = button.getBoundingClientRect();
            return { width: box.width, height: box.height, left: box.left, right: box.right };
          }),
        })"""
    )
    assert max(dimensions["html"], dimensions["body"]) <= dimensions["client"] + 1
    assert dimensions["buttons"]
    assert all(button["height"] >= 44 for button in dimensions["buttons"])
    assert all(button["left"] >= -1 and button["right"] <= width + 1 for button in dimensions["buttons"])


def test_native_template_is_responsive_standalone_and_embedded(tmp_path: Path):
    sync_api = pytest.importorskip("playwright.sync_api")
    executable = browser_executable()
    if not executable:
        pytest.skip("没有可用的 Chromium/Chrome，真实浏览器响应式验收由发布环境执行")

    page_html = tmp_path / "index.html"
    page_html.write_text(TEMPLATE_HTML.read_text(encoding="utf-8"), encoding="utf-8")
    wrapper = tmp_path / "embedded.html"
    wrapper.write_text(
        '<!doctype html><meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        f'<iframe title="业务模块" src="{page_html.as_uri()}" style="border:0;width:100vw;height:100vh"></iframe>',
        encoding="utf-8",
    )

    tested_engines: list[str] = []
    with sync_api.sync_playwright() as playwright:
        for engine_name in ("chromium", "webkit", "firefox"):
            browser_type = getattr(playwright, engine_name)
            try:
                browser = browser_type.launch(
                    headless=True,
                    **({"executable_path": executable} if engine_name == "chromium" else {}),
                )
            except sync_api.Error:
                continue
            tested_engines.append(engine_name)
            try:
                page = browser.new_page(viewport={"width": 390, "height": 844})
                page.goto(page_html.as_uri())
                for width, height in VIEWPORTS:
                    assert_viewport(page, width, height)
                page.set_viewport_size({"width": 390, "height": 844})
                page.locator("#status").evaluate("(node) => node.dataset.unsaved = '保留' ")
                page.set_viewport_size({"width": 844, "height": 390})
                assert page.locator("#status").get_attribute("data-unsaved") == "保留"
                page.get_by_role("button", name="打开业务导航").click()
                page.get_by_role("navigation", name="业务导航").wait_for()
                page.go_back()
                assert page.get_by_role("button", name="打开业务导航").get_attribute("aria-expanded") == "false"

                page.goto(wrapper.as_uri())
                frame = page.frame_locator('iframe[title="业务模块"]')
                frame.locator("body").wait_for()
                assert frame.locator(".bar").evaluate("(node) => getComputedStyle(node).display") == "none"
                for width, height in VIEWPORTS:
                    page.set_viewport_size({"width": width, "height": height})
                    page.wait_for_timeout(50)
                    overflow = frame.locator("body").evaluate(
                        "(body) => Math.max(body.scrollWidth, document.documentElement.scrollWidth) - document.documentElement.clientWidth"
                    )
                    assert overflow <= 1
            finally:
                browser.close()
    assert "chromium" in tested_engines
