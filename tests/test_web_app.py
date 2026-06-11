from __future__ import annotations

import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from ai_rpa.desktop_app import prepare_desktop_data_dir
from ai_rpa.web_app import (
    apply_agent_decision,
    fallback_agent_decision,
    list_workflow_names,
    normalize_selector,
    static_content_type,
    workflow_path,
)


class WebAppTest(unittest.TestCase):
    def test_lists_workflows(self) -> None:
        workflows = list_workflow_names()
        self.assertIn("local_login_demo.json", workflows)

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(ValueError):
            workflow_path("../README.md")

    def test_static_content_types_are_browser_safe(self) -> None:
        self.assertEqual(static_content_type(Path("app.html")), "text/html; charset=utf-8")
        self.assertEqual(static_content_type(Path("app.css")), "text/css; charset=utf-8")
        self.assertEqual(static_content_type(Path("app.js")), "application/javascript; charset=utf-8")

    def test_agent_can_insert_valid_node(self) -> None:
        workflow = sample_workflow()
        result = apply_agent_decision(
            {
                "action": "insert_node",
                "reply": "已添加点击节点",
                "insertAfterIndex": 0,
                "node": {
                    "id": "click-submit",
                    "type": "web.click",
                    "title": "点击提交",
                    "params": {"target": "提交", "selector": "button"},
                },
            },
            workflow,
            0,
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["selectedIndex"], 1)
        self.assertEqual(result["workflow"]["nodes"][1]["type"], "web.click")

    def test_agent_rejects_unknown_node_type(self) -> None:
        with self.assertRaises(ValueError):
            apply_agent_decision(
                {
                    "action": "insert_node",
                    "node": {"id": "macro", "type": "macro.do_all", "title": "宏节点", "params": {}},
                },
                sample_workflow(),
                0,
            )

    def test_fallback_generates_specific_node(self) -> None:
        decision = fallback_agent_decision("添加一个点击按钮节点", sample_workflow(), 0)
        self.assertEqual(decision["action"], "insert_node")
        self.assertEqual(decision["node"]["type"], "web.click")

    def test_agent_normalizes_jquery_contains_selector(self) -> None:
        self.assertEqual(
            normalize_selector("button:contains('登录')"),
            'button:has-text("登录")',
        )

    def test_frontend_reports_mimo_error_without_blocking_overlay(self) -> None:
        app_js = Path(__file__).resolve().parents[1] / "static" / "app.js"
        app_html = Path(__file__).resolve().parents[1] / "static" / "app.html"
        content = app_js.read_text(encoding="utf-8")
        html = app_html.read_text(encoding="utf-8")
        self.assertNotIn("mimoBlockingOverlay", html)
        self.assertNotIn('overlay.classList.remove("hidden")', content)
        self.assertNotIn('document.body.classList.add("is-blocked")', content)
        self.assertIn("function isMimoError(detail)", content)
        self.assertIn("maybeReportMimoError(settings.mimoError || \"\")", content)
        self.assertIn('if (step.status === "fail") maybeReportMimoError(step.detail || "")', content)
        self.assertNotIn('return /mimo/i.test(text)', content)

    def test_settings_api_exposes_mimo_error_flag(self) -> None:
        web_app_py = Path(__file__).resolve().parents[1] / "src" / "ai_rpa" / "web_app.py"
        content = web_app_py.read_text(encoding="utf-8")
        self.assertIn('"mimoHealthy": mimo_healthy', content)
        self.assertIn('"mimoError": "" if mimo_healthy else mimo_error', content)

    def test_mimo_client_supports_health_check(self) -> None:
        mimo_client_py = Path(__file__).resolve().parents[1] / "src" / "ai_rpa" / "mimo_client.py"
        content = mimo_client_py.read_text(encoding="utf-8")
        self.assertIn("def health_check(self, timeout: int = 15)", content)

    def test_desktop_prepares_writable_workflow_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            with patch.dict(os.environ, {"AI_RPA_USER_DATA_DIR": temp_dir}, clear=False):
                os.environ.pop("AI_RPA_WORKFLOW_DIR", None)
                os.environ.pop("AI_RPA_BROWSER_PROFILE", None)
                os.environ.pop("AI_RPA_SCREENSHOT_DIR", None)
                data_dir = prepare_desktop_data_dir()
                workflow_dir = Path(os.environ["AI_RPA_WORKFLOW_DIR"])
                self.assertEqual(data_dir, Path(temp_dir).resolve())
                self.assertEqual(workflow_dir, data_dir / "workflows")
                self.assertTrue((workflow_dir / "dianxiaomi_ai_workflow.json").is_file())
                self.assertTrue((data_dir / ".env.example").is_file())


def sample_workflow() -> dict[str, object]:
    return {
        "id": "sample",
        "name": "示例流程",
        "version": "0.1.0",
        "nodes": [
            {
                "id": "open",
                "type": "web.open",
                "title": "打开网页",
                "params": {"url": "https://example.com"},
            }
        ],
    }


if __name__ == "__main__":
    unittest.main()
