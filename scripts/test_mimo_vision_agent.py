from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from playwright.async_api import async_playwright

from ai_rpa.mimo_client import MimoClient
from ai_rpa.web_app import APP_ROOT, normalize_selector


CAPTCHA_PATTERN = re.compile(r"验证码|校验码|图形码|captcha|verify|vcode", re.IGNORECASE)

SYSTEM_PROMPT = """你是一个 RPA 视觉判断 Agent。
你会收到网页截图和任务目标，请识别页面内容，并返回可以执行的安全操作。

只返回 JSON，不要输出 Markdown。格式：
{
  "pageSummary": "简短说明你在截图里看到了什么",
  "confidence": 0.0,
  "actions": [
    {"type": "fill", "target": "账号", "selector": "input[name=account]", "value": "demo_user"},
    {"type": "fill", "target": "密码", "selector": "input[name=password]", "value": "demo_password"},
    {"type": "click", "target": "登录", "selector": "button[type=submit]"}
  ],
  "avoid": ["验证码"]
}

约束：
- 不允许点击、输入、读取验证码或 captcha 类元素。
- selector 必须使用 Playwright/CSS 可执行形式，不要使用 jQuery 的 :contains()。
- 如果能看出输入框和按钮，请给出具体 selector。
- 只生成完成任务所需的最少动作。
"""

USER_PROMPT = """请根据截图判断页面内容，并生成登录操作。
账号填写 demo_user，密码填写 demo_password。
不要点击或填写验证码。"""


async def main() -> None:
    parser = argparse.ArgumentParser(description="Test Mimo vision-driven RPA on the local login fixture.")
    parser.add_argument("--headless", action="store_true", help="Run browser headless.")
    args = parser.parse_args()

    load_dotenv(APP_ROOT / ".env")
    screenshot_path = APP_ROOT / "screenshots" / "mimo_vision_login.png"
    screenshot_path.parent.mkdir(exist_ok=True)
    fixture_url = (APP_ROOT / "fixtures" / "login_demo.html").resolve().as_uri()

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless)
        page = await browser.new_page(viewport={"width": 1100, "height": 760})
        await page.goto(fixture_url, wait_until="domcontentloaded")
        await page.screenshot(path=str(screenshot_path), full_page=False)

        decision = MimoClient().complete_json(
            SYSTEM_PROMPT,
            USER_PROMPT,
            screenshot_path=str(screenshot_path),
        )
        executed = []
        for action in decision.get("actions", []):
            executed.append(await execute_action(page, action))

        result_text = await page.locator("#result").inner_text()
        await browser.close()

    payload = {
        "status": "pass" if "captcha-safe" in result_text else "fail",
        "screenshot": str(screenshot_path),
        "decision": decision,
        "executed": executed,
        "resultText": result_text,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    if payload["status"] != "pass":
        raise SystemExit(1)


async def execute_action(page: Any, action: Any) -> dict[str, str]:
    if not isinstance(action, dict):
        raise RuntimeError("action must be an object")
    action_type = str(action.get("type") or "").strip()
    target = str(action.get("target") or "")
    selector = normalize_selector(str(action.get("selector") or "").strip())
    value = str(action.get("value") or "")
    signal = " ".join([action_type, target, selector, value])
    if CAPTCHA_PATTERN.search(signal):
        raise RuntimeError(f"refusing captcha-like action: {signal}")
    if action_type not in {"fill", "click", "wait"}:
        raise RuntimeError(f"unsupported action type: {action_type}")

    if action_type == "wait":
        await page.wait_for_timeout(int(float(action.get("ms") or 1000)))
        return {"type": action_type, "target": target, "detail": "waited"}

    locator = page.locator(selector).first if selector else page.get_by_label(target).first
    if callable(locator):
        locator = locator()
    await ensure_not_captcha(locator)
    if action_type == "fill":
        await locator.fill(value)
        return {"type": action_type, "target": target, "detail": "filled"}
    await locator.click()
    return {"type": action_type, "target": target, "detail": "clicked"}


async def ensure_not_captcha(locator: Any) -> None:
    signal = await locator.evaluate(
        """element => [
          element.tagName,
          element.id,
          element.className,
          element.getAttribute('name'),
          element.getAttribute('placeholder'),
          element.getAttribute('aria-label'),
          element.getAttribute('title'),
          element.textContent
        ].filter(Boolean).join(' ')"""
    )
    if CAPTCHA_PATTERN.search(signal or ""):
        raise RuntimeError("refusing to interact with captcha-like element")


if __name__ == "__main__":
    asyncio.run(main())
