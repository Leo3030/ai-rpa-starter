from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
import unittest

from ai_rpa.executor import (
    BUSINESS_MODAL_TEXT_PATTERN,
    GLOBAL_MODAL_CLOSE_SELECTOR,
    MODAL_CONTAINER_SELECTOR,
    PlaywrightWorkflowExecutor,
    RepairOutcome,
    _OPEN_BROWSER_SESSIONS,
    apply_node_patch,
    compact_html_snapshot,
    contains_cjk,
    extract_runtime_references,
    extract_regex_groups,
    freight_template_label_for_weight,
    find_else_or_end_if,
    find_end_if,
    find_end_loop,
    get_variable_path,
    is_captcha_target,
    is_profile_busy_error,
    modal_detail,
    normalize_ai_result,
    normalize_ai_title,
    page_context_payload,
    pc_description_prefix_for_product_type,
    resolve_env_value,
    resolve_numeric_expression,
    resolve_runtime_value,
    set_variable_path,
    should_auto_close_modal,
    unresolved_runtime_references,
)
from ai_rpa.models import RunStep, Workflow, WorkflowNode
from ai_rpa.workflow_loader import WorkflowValidationError, load_workflow, parse_workflow


class WorkflowLoaderTest(unittest.TestCase):
    def test_parse_valid_workflow(self) -> None:
        workflow = parse_workflow({
            "id": "demo",
            "name": "Demo",
            "pageObjects": {
                "page1": {"name": "首页", "urlIncludes": ["/home"]}
            },
            "nodes": [
                {"id": "open", "type": "web.open", "params": {"url": "https://example.com", "pageContext": "page1"}},
                {"id": "close-modals", "type": "web.close_modals", "params": {}},
                {"id": "wait", "type": "web.wait_for", "params": {"text": "Example Domain"}},
            ],
        })
        self.assertEqual(workflow.id, "demo")
        self.assertEqual(len(workflow.nodes), 3)
        self.assertEqual(workflow.pageObjects["page1"]["name"], "首页")
        self.assertEqual(page_context_payload(workflow, workflow.nodes[0])["id"], "page1")

    def test_parse_disabled_node(self) -> None:
        workflow = parse_workflow({
            "id": "disabled-demo",
            "name": "Disabled Demo",
            "nodes": [
                {"id": "open", "type": "web.open", "disabled": True, "params": {"url": "https://example.com"}},
            ],
        })
        self.assertTrue(workflow.nodes[0].disabled)

    def test_disabled_node_is_skipped_by_executor(self) -> None:
        async def check_skip() -> None:
            workflow = Workflow(
                id="disabled-run",
                name="Disabled Run",
                version="0.1.0",
                nodes=[
                    WorkflowNode(
                        id="disabled-wait",
                        type="flow.wait",
                        title="禁用等待",
                        params={"seconds": 999},
                        disabled=True,
                    )
                ],
            )
            executor = PlaywrightWorkflowExecutor()
            executor.active_page = object()
            result = await executor.run_without_browser_for_test(workflow)
            self.assertEqual(result.status, "pass")
            self.assertEqual(result.steps[0].node_id, "disabled-wait")
            self.assertEqual(result.steps[0].detail, "skipped disabled node")

        asyncio.run(check_skip())

    def test_disabled_node_is_skipped_when_executed_directly(self) -> None:
        async def check_skip() -> None:
            node = WorkflowNode(
                id="disabled-direct",
                type="web.open",
                title="禁用打开",
                params={"url": "https://example.com"},
                disabled=True,
            )
            workflow = Workflow(
                id="disabled-direct-run",
                name="Disabled Direct Run",
                version="0.1.0",
                nodes=[node],
            )
            executor = PlaywrightWorkflowExecutor()
            result = await executor.execute_node(object(), workflow, node, {})
            self.assertEqual(result.status, "pass")
            self.assertEqual(result.node_id, "disabled-direct")
            self.assertEqual(result.detail, "skipped disabled node")

        asyncio.run(check_skip())

    def test_execute_web_scroll_node(self) -> None:
        class FakeScrollLocator:
            def __init__(self) -> None:
                self.first = self
                self.scroll_top = 0.0
                self.scroll_height = 1200.0
                self.client_height = 300.0

            async def wait_for(self, state: str = "attached", timeout: int = 10000) -> None:
                return None

            async def evaluate(self, expression: str, arg: Any = None, timeout: int = 1500) -> Any:
                if "scrollTop = Number(element.scrollHeight" in expression:
                    before = self.scroll_top
                    self.scroll_top = self.scroll_height
                    return {"before": before, "after": self.scroll_top, "max": self.scroll_height - self.client_height}
                if "element.scrollTop = 0" in expression:
                    before = self.scroll_top
                    self.scroll_top = 0.0
                    return {"before": before, "after": self.scroll_top, "max": self.scroll_height - self.client_height}
                return "scrollable list"

        class FakeScrollPage:
            def __init__(self) -> None:
                self.locator_instance = FakeScrollLocator()
                self.waits: list[int] = []
                self.last_selector = ""

            def locator(self, selector: str) -> Any:
                self.last_selector = selector
                return self.locator_instance

            async def wait_for_timeout(self, ms: int) -> None:
                self.waits.append(ms)

        class ScrollNodeExecutor(PlaywrightWorkflowExecutor):
            async def ensure_not_captcha(self, locator: Any, target: str = "元素") -> None:
                return None

        async def check_scroll() -> None:
            page = FakeScrollPage()
            workflow = Workflow(id="scroll-demo", name="Scroll Demo", version="0.1.0", nodes=[])
            node = WorkflowNode(
                id="scroll-bottom",
                type="web.scroll",
                title="滚动到底部",
                params={
                    "target": "采集箱商品列表",
                    "selector": ".vxe-table--body-wrapper",
                    "position": "bottom",
                    "scrollSteps": 2,
                    "settleMs": 50,
                },
            )
            executor = ScrollNodeExecutor()
            result = await executor.execute_node(page, workflow, node, {})
            self.assertEqual(result.status, "pass")
            self.assertEqual(page.last_selector, ".vxe-table--body-wrapper")
            self.assertEqual(page.locator_instance.scroll_top, 1200.0)
            self.assertEqual(page.waits, [50, 50])

        asyncio.run(check_scroll())

    def test_stop_after_is_honored_after_successful_repair_retry(self) -> None:
        class FakeBrowserContext:
            pages = [object()]

        class RepairRetryExecutor(PlaywrightWorkflowExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.attempts = 0
                self.modal_guard_enabled = False

            async def execute_node(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> RunStep:
                self.attempts += 1
                if node.id == "stop-node" and self.attempts == 1:
                    return RunStep(node_id=node.id, title=node.title, status="fail", detail="first attempt failed")
                return RunStep(node_id=node.id, title=node.title, status="pass", detail="ok")

            async def try_repair_failed_node(
                self,
                page: Any,
                workflow: Workflow,
                node_index: int,
                node: WorkflowNode,
                failed_step: RunStep,
            ) -> RepairOutcome:
                return RepairOutcome(
                    step=RunStep(
                        node_id=f"{node.id}-ai-repair",
                        title="AI 修复 workflow",
                        status="pass",
                        detail="patched",
                    ),
                    applied=True,
                )

        async def check_stop_after() -> None:
            workflow = Workflow(
                id="stop-after-repair",
                name="Stop After Repair",
                version="0.1.0",
                nodes=[
                    WorkflowNode(
                        id="stop-node",
                        type="web.wait_for",
                        title="停在这里",
                        params={"selector": "#ready", "stopAfter": True},
                    ),
                    WorkflowNode(
                        id="after-stop",
                        type="flow.wait",
                        title="不应执行",
                        params={"seconds": 1},
                    ),
                ],
            )
            executor = RepairRetryExecutor()
            executor.active_page = object()
            _OPEN_BROWSER_SESSIONS.append({"playwright": object(), "context": FakeBrowserContext()})
            try:
                result = await executor.run(workflow)
            finally:
                _OPEN_BROWSER_SESSIONS.clear()
            self.assertEqual(result.status, "pass")
            self.assertEqual(
                [step.node_id for step in result.steps],
                ["browser-session", "stop-node", "stop-node-ai-repair", "stop-node", "browser-session"],
            )

        asyncio.run(check_stop_after())

    def test_product_loop_skips_to_next_item_on_missing_remark(self) -> None:
        class SkipRemarkExecutor(PlaywrightWorkflowExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.modal_guard_enabled = False

            async def evaluate_condition(self, page: Any, node: WorkflowNode) -> bool:
                return True

            async def execute_node(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> RunStep:
                if node.id == "extract-first-product-remark":
                    return RunStep(node_id=node.id, title=node.title, status="fail", detail="extracted text did not match regex: 备注")
                return RunStep(node_id=node.id, title=node.title, status="pass", detail="ok")

        async def check_skip() -> None:
            workflow = Workflow(
                id="skip-missing-remark",
                name="Skip Missing Remark",
                version="0.1.0",
                nodes=[
                    WorkflowNode(id="loop-products", type="flow.loop", title="循环处理商品", params={"times": 2}),
                    WorkflowNode(id="if-edit-button-visible", type="flow.if", title="如果存在编辑按钮", params={"selector": "#edit"}),
                    WorkflowNode(id="extract-first-product-remark", type="web.extract", title="读取备注", params={"selector": "#remark"}),
                    WorkflowNode(id="click-first-edit", type="web.click", title="点击编辑", params={"selector": "#edit"}),
                    WorkflowNode(id="end-if-edit-button-visible", type="flow.end_if", title="结束商品存在判断", params={}),
                    WorkflowNode(id="end-loop-products", type="flow.end_loop", title="结束商品循环", params={}),
                ],
            )
            executor = SkipRemarkExecutor()
            executor.active_page = object()
            result = await executor.run_without_browser_for_test(workflow)
            self.assertEqual(result.status, "pass")
            self.assertIn("extract-first-product-remark-skip-product", [step.node_id for step in result.steps])
            skip_step = next(step for step in result.steps if step.node_id == "extract-first-product-remark-skip-product")
            self.assertIn("失败节点=extract-first-product-remark", skip_step.detail)
            self.assertIn("extracted text did not match regex: 备注", skip_step.detail)
            self.assertEqual([step.node_id for step in result.steps].count("end-loop-products"), 2)

        asyncio.run(check_skip())

    def test_product_loop_skips_to_next_item_on_edit_failure(self) -> None:
        class SkipEditFailureExecutor(PlaywrightWorkflowExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.modal_guard_enabled = False

            async def evaluate_condition(self, page: Any, node: WorkflowNode) -> bool:
                return True

            async def execute_node(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> RunStep:
                if node.id == "write-translated-title":
                    return RunStep(node_id=node.id, title=node.title, status="fail", detail="failed while editing detail page")
                return RunStep(node_id=node.id, title=node.title, status="pass", detail="ok")

        async def check_skip() -> None:
            workflow = Workflow(
                id="skip-edit-failure",
                name="Skip Edit Failure",
                version="0.1.0",
                nodes=[
                    WorkflowNode(id="loop-products", type="flow.loop", title="循环处理商品", params={"times": 2}),
                    WorkflowNode(id="if-edit-button-visible", type="flow.if", title="如果存在编辑按钮", params={"selector": "#edit"}),
                    WorkflowNode(id="extract-first-product-remark", type="web.extract", title="读取备注", params={"selector": "#remark"}),
                    WorkflowNode(id="click-first-edit", type="web.click", title="点击编辑", params={"selector": "#edit"}),
                    WorkflowNode(id="write-translated-title", type="web.input", title="写入标题", params={"selector": "#title", "value": "English Title"}),
                    WorkflowNode(id="end-if-edit-button-visible", type="flow.end_if", title="结束商品存在判断", params={}),
                    WorkflowNode(id="end-loop-products", type="flow.end_loop", title="结束商品循环", params={}),
                ],
            )
            executor = SkipEditFailureExecutor()
            executor.active_page = object()
            result = await executor.run_without_browser_for_test(workflow)
            self.assertEqual(result.status, "pass")
            self.assertIn("write-translated-title-skip-product", [step.node_id for step in result.steps])
            skip_step = next(step for step in result.steps if step.node_id == "write-translated-title-skip-product")
            self.assertIn("失败节点=write-translated-title", skip_step.detail)
            self.assertIn("failed while editing detail page", skip_step.detail)
            self.assertEqual([step.node_id for step in result.steps].count("end-loop-products"), 2)

        asyncio.run(check_skip())

    def test_product_loop_skip_closes_detail_tab_and_returns_to_list(self) -> None:
        class FakeTabPage:
            def __init__(self, url: str, context: Any) -> None:
                self.url = url
                self.context = context
                self.closed = False
                self.brought_to_front = False

            async def close(self) -> None:
                self.closed = True

            async def bring_to_front(self) -> None:
                self.brought_to_front = True

            async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
                return None

            async def wait_for_timeout(self, milliseconds: int) -> None:
                return None

        class FakeTabContext:
            def __init__(self) -> None:
                self.pages: list[FakeTabPage] = []

        class SkipDetailFailureExecutor(PlaywrightWorkflowExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.modal_guard_enabled = False

            async def evaluate_condition(self, page: Any, node: WorkflowNode) -> bool:
                return True

            async def execute_node(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> RunStep:
                if node.id == "write-translated-title":
                    return RunStep(node_id=node.id, title=node.title, status="fail", detail="detail page write failed")
                return RunStep(node_id=node.id, title=node.title, status="pass", detail="ok")

        async def check_skip_closes_tab() -> None:
            context = FakeTabContext()
            list_page = FakeTabPage("https://www.dianxiaomi.com/web/smt/smtProductList/draft", context)
            detail_page = FakeTabPage("https://www.dianxiaomi.com/web/smt/editProduct?id=123", context)
            context.pages = [list_page, detail_page]
            workflow = Workflow(
                id="skip-detail-tab",
                name="Skip Detail Tab",
                version="0.1.0",
                nodes=[
                    WorkflowNode(id="loop-products", type="flow.loop", title="循环处理商品", params={"times": 1}),
                    WorkflowNode(id="if-edit-button-visible", type="flow.if", title="如果存在编辑按钮", params={"selector": "#edit"}),
                    WorkflowNode(id="write-translated-title", type="web.input", title="写入标题", params={"selector": "#title", "value": "English Title"}),
                    WorkflowNode(id="end-if-edit-button-visible", type="flow.end_if", title="结束商品存在判断", params={}),
                    WorkflowNode(id="end-loop-products", type="flow.end_loop", title="结束商品循环", params={}),
                ],
                pageObjects={
                    "page2": {"urlIncludes": ["/web/smt/smtProductList/draft"]},
                    "page3": {"urlExcludes": ["/web/smt/smtProductList/draft"]},
                },
            )
            executor = SkipDetailFailureExecutor()
            executor.active_page = detail_page
            result = await executor.run_without_browser_for_test(workflow)
            self.assertEqual(result.status, "pass")
            self.assertTrue(detail_page.closed)
            self.assertIs(executor.active_page, list_page)
            self.assertTrue(list_page.brought_to_front)
            skip_step = next(step for step in result.steps if step.node_id == "write-translated-title-skip-product")
            self.assertIn("已关闭当前详情页并返回列表页", skip_step.detail)

        asyncio.run(check_skip_closes_tab())

    def test_product_loop_skips_to_next_item_when_remark_missing(self) -> None:
        class MissingRemarkExecutor(PlaywrightWorkflowExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.modal_guard_enabled = False

            async def evaluate_condition(self, page: Any, node: WorkflowNode) -> bool:
                return True

            async def execute_node(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> RunStep:
                if node.id == "extract-first-product-remark":
                    return RunStep(node_id=node.id, title=node.title, status="fail", detail="未找到可操作元素：第1条商品备注")
                return RunStep(node_id=node.id, title=node.title, status="pass", detail="ok")

        async def check_skip() -> None:
            workflow = Workflow(
                id="skip-missing-remark-selector",
                name="Skip Missing Remark Selector",
                version="0.1.0",
                nodes=[
                    WorkflowNode(id="loop-products", type="flow.loop", title="循环处理商品", params={"times": 2}),
                    WorkflowNode(id="if-edit-button-visible", type="flow.if", title="如果存在编辑按钮", params={"selector": "#edit"}),
                    WorkflowNode(id="extract-first-product-remark", type="web.extract", title="读取备注", params={"selector": "#remark"}),
                    WorkflowNode(id="click-first-edit", type="web.click", title="点击编辑", params={"selector": "#edit"}),
                    WorkflowNode(id="end-if-edit-button-visible", type="flow.end_if", title="结束商品存在判断", params={}),
                    WorkflowNode(id="end-loop-products", type="flow.end_loop", title="结束商品循环", params={}),
                ],
            )
            executor = MissingRemarkExecutor()
            executor.active_page = object()
            result = await executor.run_without_browser_for_test(workflow)
            self.assertEqual(result.status, "pass")
            self.assertIn("extract-first-product-remark-skip-product", [step.node_id for step in result.steps])
            skip_step = next(step for step in result.steps if step.node_id == "extract-first-product-remark-skip-product")
            self.assertIn("失败节点=extract-first-product-remark", skip_step.detail)
            self.assertIn("未找到可操作元素：第1条商品备注", skip_step.detail)
            self.assertEqual([step.node_id for step in result.steps].count("end-loop-products"), 2)

        asyncio.run(check_skip())

    def test_product_loop_skips_to_next_item_when_remark_format_is_invalid(self) -> None:
        class InvalidRemarkExecutor(PlaywrightWorkflowExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.modal_guard_enabled = False

            async def evaluate_condition(self, page: Any, node: WorkflowNode) -> bool:
                return True

            async def execute_node(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> RunStep:
                if node.id == "extract-first-product-remark":
                    return RunStep(node_id=node.id, title=node.title, status="fail", detail="extracted text did not match regex: 备注")
                return RunStep(node_id=node.id, title=node.title, status="pass", detail="ok")

        async def check_skip() -> None:
            workflow = Workflow(
                id="skip-invalid-remark-format",
                name="Skip Invalid Remark Format",
                version="0.1.0",
                nodes=[
                    WorkflowNode(id="loop-products", type="flow.loop", title="循环处理商品", params={"times": 2}),
                    WorkflowNode(id="if-edit-button-visible", type="flow.if", title="如果存在编辑按钮", params={"selector": "#edit"}),
                    WorkflowNode(id="extract-first-product-remark", type="web.extract", title="读取备注", params={"selector": "#remark"}),
                    WorkflowNode(id="click-first-edit", type="web.click", title="点击编辑", params={"selector": "#edit"}),
                    WorkflowNode(id="end-if-edit-button-visible", type="flow.end_if", title="结束商品存在判断", params={}),
                    WorkflowNode(id="end-loop-products", type="flow.end_loop", title="结束商品循环", params={}),
                ],
            )
            executor = InvalidRemarkExecutor()
            executor.active_page = object()
            result = await executor.run_without_browser_for_test(workflow)
            self.assertEqual(result.status, "pass")
            self.assertIn("extract-first-product-remark-skip-product", [step.node_id for step in result.steps])
            self.assertEqual([step.node_id for step in result.steps].count("end-loop-products"), 2)

        asyncio.run(check_skip())

    def test_product_loop_resolves_nth_matched_selector_each_iteration(self) -> None:
        class CaptureLoopSelectorExecutor(PlaywrightWorkflowExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.modal_guard_enabled = False
                self.resolved_selectors: list[str] = []

            async def evaluate_condition(self, page: Any, node: WorkflowNode) -> bool:
                return True

            async def execute_node(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> RunStep:
                selector = str(node.params.get("selector") or "")
                if selector:
                    self.resolved_selectors.append(resolve_runtime_value(selector, variables))
                return RunStep(node_id=node.id, title=node.title, status="pass", detail="ok")

        async def check_loop_variables() -> None:
            workflow = Workflow(
                id="loop-nth-match-selectors",
                name="Loop Nth Match Selectors",
                version="0.1.0",
                nodes=[
                    WorkflowNode(id="loop-products", type="flow.loop", title="循环处理商品", params={"times": 3}),
                    WorkflowNode(
                        id="extract-first-product-remark",
                        type="web.extract",
                        title="读取备注",
                        params={"selector": ":nth-match(table tbody tr .comment, ${loop-products.index})"},
                    ),
                    WorkflowNode(id="end-loop-products", type="flow.end_loop", title="结束商品循环", params={}),
                ],
            )
            executor = CaptureLoopSelectorExecutor()
            executor.active_page = object()
            result = await executor.run_without_browser_for_test(workflow)
            self.assertEqual(result.status, "pass")
            self.assertEqual([step.node_id for step in result.steps].count("loop-products"), 3)
            self.assertEqual(
                executor.resolved_selectors,
                [
                    ":nth-match(table tbody tr .comment, 1)",
                    ":nth-match(table tbody tr .comment, 2)",
                    ":nth-match(table tbody tr .comment, 3)",
                ],
            )

        asyncio.run(check_loop_variables())

    def test_evaluate_condition_supports_scroll_before_locate(self) -> None:
        class FakeLocator:
            def __init__(self, page: Any) -> None:
                self.page = page
                self.first = self

            async def is_visible(self, timeout: int = 1000) -> bool:
                return bool(self.page.visible)

        class FakePage:
            def __init__(self) -> None:
                self.visible = False
                self.last_selector = ""

            def locator(self, selector: str) -> Any:
                self.last_selector = selector
                return FakeLocator(self)

        class ScrollAwareExecutor(PlaywrightWorkflowExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.modal_guard_enabled = False
                self.scrolled_selector = ""

            async def scroll_before_locate(self, page: Any, node: WorkflowNode, target_selector: str) -> None:
                self.scrolled_selector = target_selector
                page.visible = True

        async def check_scroll_before_locate() -> None:
            page = FakePage()
            executor = ScrollAwareExecutor()
            node = WorkflowNode(
                id="if-visible-after-scroll",
                type="flow.if",
                title="滚动后判断可见",
                params={
                    "selector": ".target-row .edit-link",
                    "scrollBeforeLocate": True,
                    "scrollBeforeLocateContainer": ".vxe-table--body-wrapper",
                },
            )
            result = await executor.evaluate_condition(page, node)
            self.assertTrue(result)
            self.assertEqual(page.last_selector, ".target-row .edit-link")
            self.assertEqual(executor.scrolled_selector, ".target-row .edit-link")

        asyncio.run(check_scroll_before_locate())

    def test_dianxiaomi_elasticity_steps_are_enabled(self) -> None:
        workflow = load_workflow(Path("workflows/dianxiaomi_ai_workflow.json"))
        nodes_by_id = {node.id: node for node in workflow.nodes}
        self.assertFalse(nodes_by_id["click-elasticity-dropdown"].disabled)
        self.assertFalse(nodes_by_id["select-medium-stretch"].disabled)

    def test_reject_duplicate_node_id(self) -> None:
        with self.assertRaises(WorkflowValidationError):
            parse_workflow({
                "id": "demo",
                "name": "Demo",
                "nodes": [
                    {"id": "same", "type": "web.open", "params": {"url": "https://example.com"}},
                    {"id": "same", "type": "flow.wait", "params": {"seconds": 1}},
                ],
            })

    def test_reject_unknown_node_type(self) -> None:
        with self.assertRaises(WorkflowValidationError):
            parse_workflow({
                "id": "demo",
                "name": "Demo",
                "nodes": [{"id": "bad", "type": "macro.do_everything", "params": {}}],
            })

    def test_captcha_targets_are_recognized(self) -> None:
        self.assertTrue(is_captcha_target("验证码"))
        self.assertTrue(is_captcha_target("captcha input"))
        self.assertFalse(is_captcha_target("登录"))

    def test_profile_busy_error_is_recognized(self) -> None:
        self.assertTrue(is_profile_busy_error(RuntimeError("正在现有的浏览器会话中打开")))
        self.assertTrue(is_profile_busy_error(RuntimeError("Target page, context or browser has been closed")))
        self.assertFalse(is_profile_busy_error(RuntimeError("selector not found")))

    def test_close_tab_switches_back_to_list_page_context(self) -> None:
        class FakeTabPage:
            def __init__(self, url: str, context: Any) -> None:
                self.url = url
                self.context = context
                self.closed = False
                self.brought_to_front = False

            async def close(self) -> None:
                self.closed = True

            async def bring_to_front(self) -> None:
                self.brought_to_front = True

            async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
                return None

            async def wait_for_timeout(self, milliseconds: int) -> None:
                return None

        class FakeTabContext:
            def __init__(self) -> None:
                self.pages: list[FakeTabPage] = []

        async def check_close_tab() -> None:
            context = FakeTabContext()
            list_page = FakeTabPage("https://www.dianxiaomi.com/web/smt/smtProductList/draft", context)
            detail_page = FakeTabPage("https://www.dianxiaomi.com/web/smt/editProduct?id=123", context)
            context.pages = [list_page, detail_page]
            workflow = Workflow(
                id="close-tab",
                name="Close Tab",
                version="0.1.0",
                nodes=[
                    WorkflowNode(
                        id="close-detail-tab-back-to-list",
                        type="web.close_tab",
                        title="关闭当前商品标签并返回列表",
                        params={"pageContext": "page3", "switchToPageContext": "page2"},
                    )
                ],
                pageObjects={
                    "page2": {"urlIncludes": ["/web/smt/smtProductList/draft"]},
                    "page3": {"urlExcludes": ["/web/smt/smtProductList/draft"]},
                },
            )
            executor = PlaywrightWorkflowExecutor()
            executor.modal_guard_enabled = False
            executor.active_page = detail_page
            result = await executor.execute_node(detail_page, workflow, workflow.nodes[0], {})
            self.assertEqual(result.status, "pass")
            self.assertTrue(detail_page.closed)
            self.assertIs(executor.active_page, list_page)
            self.assertTrue(list_page.brought_to_front)

        asyncio.run(check_close_tab())

    def test_env_value_resolution(self) -> None:
        self.assertEqual(resolve_env_value("plain"), "plain")

    def test_runtime_variable_resolution(self) -> None:
        variables = {}
        set_variable_path(variables, "ai-title.title", "English Product Title")
        self.assertEqual(get_variable_path(variables, "ai-title.title"), "English Product Title")
        self.assertEqual(resolve_runtime_value("${ai-title.title}", variables), "English Product Title")
        self.assertEqual(resolve_runtime_value("标题：${ai-title.title}", variables), "标题：English Product Title")
        self.assertEqual(resolve_runtime_value("${freightTemplateByWeight(productRemark.weight)}", {"productRemark": {"weight": "400"}}), "351-450g")
        self.assertEqual(extract_runtime_references("${freightTemplateByWeight(productRemark.weight)}"), ["productRemark.weight"])
        self.assertEqual(extract_runtime_references("${ai-title.title}"), ["ai-title.title"])
        self.assertEqual(unresolved_runtime_references("${missing.title}", variables), ["missing.title"])

    def test_numeric_expression_resolution(self) -> None:
        variables = {"productRemark": {"costPrice": "80", "weight": "400"}}
        self.assertEqual(resolve_numeric_expression("(${productRemark.costPrice} + (${productRemark.weight} * 0.081 + 19)) * 1.2 / 6.3 / 0.92 / 0.95 * 2", variables), "57.27361882968290290944753188")
        self.assertEqual(resolve_numeric_expression("${productRemark.costPrice} * 1.5", variables), "120")
        self.assertEqual(resolve_numeric_expression("${productRemark.weight} / 1000", variables), "0.4")
        self.assertEqual(freight_template_label_for_weight("300"), "251-350g")
        self.assertEqual(freight_template_label_for_weight("400"), "351-450g")

    def test_extract_regex_groups_for_product_remark(self) -> None:
        remark = " 备注:连衣裙均码/59元/450克，云朵家"
        extracted = extract_regex_groups(
            remark,
            r"备注\s*[:：]\s*(?P<type>[^/，,]+?)\s*/\s*(?P<price>(?P<costPrice>\d+(?:\.\d+)?)\s*元)\s*/\s*(?P<weightText>(?P<weight>\d+(?:\.\d+)?)\s*克)(?:\s*[,，].*)?",
            {
                "type": "productRemark.type",
                "costPrice": "productRemark.costPrice",
                "price": "productRemark.price",
                "weight": "productRemark.weight",
                "weightText": "productRemark.weightText",
            },
        )
        self.assertEqual(extracted["productRemark.type"], "连衣裙均码")
        self.assertEqual(extracted["productRemark.costPrice"], "59")
        self.assertEqual(extracted["productRemark.price"], "59元")
        self.assertEqual(extracted["productRemark.weight"], "450")
        self.assertEqual(extracted["productRemark.weightText"], "450克")

    def test_pc_description_prefix_for_product_type(self) -> None:
        dress_one_size = pc_description_prefix_for_product_type("连衣裙均码")
        self.assertIn("One Size", dress_one_size)
        self.assertIn("Length:cm", dress_one_size)
        dress_multi = pc_description_prefix_for_product_type("连衣裙多尺码")
        self.assertIn("Size Chart", dress_multi)
        self.assertIn("XXL", dress_multi)
        self.assertIn("Prompt:", dress_multi)
        skirt_one_size = pc_description_prefix_for_product_type(" 半身裙均码 ")
        self.assertIn("One Size", skirt_one_size)
        self.assertIn("Warm Prompt:", skirt_one_size)
        set_multi = pc_description_prefix_for_product_type("套装多尺码")
        self.assertIn("TOP", set_multi)
        self.assertIn("Bottom", set_multi)
        self.assertEqual(pc_description_prefix_for_product_type("未知类型"), "")

    def test_ai_title_result_normalization(self) -> None:
        node = WorkflowNode(
            id="ai-title-translation",
            type="ai.ask",
            title="AI 翻译标题",
            params={"expectedFormat": {"title": "英文标题"}},
        )
        normalized = normalize_ai_result({"englishTitle": "\"Elegant Knit Dress With Belt\""}, node)
        self.assertEqual(normalized["title"], "Elegant Knit Dress With Belt")
        self.assertLess(len(normalize_ai_title("A " * 80)), 100)
        self.assertTrue(contains_cjk("Elegant 荷叶边 Dress"))
        self.assertFalse(contains_cjk("Elegant Ruffle Dress"))
        strict_node = WorkflowNode(
            id="ai-title-translation",
            type="ai.ask",
            title="AI 翻译标题",
            params={"expectedFormat": {"title": "英文标题"}, "rejectCjkTitle": True},
        )
        with self.assertRaisesRegex(RuntimeError, "CJK"):
            normalize_ai_result({"englishTitle": "Elegant 荷叶边 Dress"}, strict_node)

    def test_compact_html_snapshot_keeps_form_structure(self) -> None:
        html = """
        <html><head><style>.x{}</style><script>alert(1)</script></head>
        <body><label>商品标题</label><input name="subject" value="中文标题" onclick="x()" /></body></html>
        """
        compact = compact_html_snapshot(html, 120)
        self.assertIn("商品标题", compact)
        self.assertIn('name="subject"', compact)
        self.assertNotIn("<script", compact)
        self.assertNotIn("onclick", compact)

    def test_apply_node_patch_updates_safe_params_only(self) -> None:
        node = WorkflowNode(
            id="fill-title",
            type="web.input",
            title="写入标题",
            params={"selector": "input.old", "value": "Title"},
        )
        changed = apply_node_patch(node, {"params": {"selector": "input[name=subject]", "unknown": "ignored"}})
        self.assertEqual(changed, ["params.selector"])
        self.assertEqual(node.params["selector"], "input[name=subject]")
        self.assertNotIn("unknown", node.params)

    def test_modal_detail(self) -> None:
        self.assertEqual(modal_detail(0), "")
        self.assertEqual(modal_detail(2), "; closed 2 modal(s)")

    def test_modal_guard_can_be_suspended_for_user_opened_modals(self) -> None:
        async def check_guard() -> None:
            executor = PlaywrightWorkflowExecutor()
            executor.arm_modal_guard()
            self.assertTrue(executor.modal_guard_active())
            executor.suspend_modal_guard()
            self.assertFalse(executor.modal_guard_active())

        asyncio.run(check_guard())

    def test_should_auto_close_modal_only_for_dismiss_only_popups(self) -> None:
        self.assertTrue(
            should_auto_close_modal(
                text="平台公告",
                button_texts=["知道了"],
                has_form_fields=False,
                has_structured_content=False,
                has_dismiss_control=True,
            )
        )
        self.assertFalse(
            should_auto_close_modal(
                text="编辑分类 去编辑产品",
                button_texts=["编辑分类", "跳过"],
                has_form_fields=False,
                has_structured_content=False,
                has_dismiss_control=True,
            )
        )
        self.assertFalse(
            should_auto_close_modal(
                text="确定删除全部自定义属性吗",
                button_texts=["取消", "确定"],
                has_form_fields=False,
                has_structured_content=False,
                has_dismiss_control=True,
            )
        )
        self.assertIsNotNone(BUSINESS_MODAL_TEXT_PATTERN.search("编辑分类"))

    def test_close_modals_uses_global_close_button_without_standard_container(self) -> None:
        async def check_close() -> None:
            executor = PlaywrightWorkflowExecutor()
            page = FakeNonstandardModalPage()
            closed = await executor.close_page_modals(page)
            self.assertEqual(closed, 1)
            self.assertTrue(page.closed)
            self.assertFalse(page.escape_pressed)

        asyncio.run(check_close())

    def test_close_modals_skips_business_modal(self) -> None:
        async def check_close() -> None:
            executor = PlaywrightWorkflowExecutor()
            page = FakeBusinessModalPage()
            closed = await executor.close_page_modals(page)
            self.assertEqual(closed, 0)
            self.assertFalse(page.closed)

        asyncio.run(check_close())

    def test_click_popup_does_not_rearm_modal_guard(self) -> None:
        class FakePopupLocator:
            def __init__(self) -> None:
                self.clicked = False

            async def click(self) -> None:
                self.clicked = True

        class FakePopupContextManager:
            def __init__(self, popup_page: Any) -> None:
                self.popup_page = popup_page
                self.value = asyncio.Future()
                self.value.set_result(popup_page)

            async def __aenter__(self) -> "FakePopupContextManager":
                return self

            async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
                return False

        class FakePopupContext:
            def __init__(self, popup_page: Any) -> None:
                self.popup_page = popup_page

            def expect_page(self, timeout: int = 0) -> FakePopupContextManager:
                return FakePopupContextManager(self.popup_page)

        class FakePopupPage:
            def __init__(self, url: str) -> None:
                self.url = url
                self.escape_pressed = False
                self.context = FakePopupContext(self)
                self.keyboard = FakeKeyboard(self)  # type: ignore[arg-type]

            async def bring_to_front(self) -> None:
                return None

            async def wait_for_load_state(self, state: str, timeout: int = 0) -> None:
                return None

            async def wait_for_timeout(self, milliseconds: int) -> None:
                return None

        class ClickExecutor(PlaywrightWorkflowExecutor):
            def __init__(self, locator: Any) -> None:
                super().__init__()
                self.modal_guard_enabled = False
                self._locator = locator

            async def locator_for(self, page: Any, node: WorkflowNode) -> Any:
                return self._locator

        async def check_click() -> None:
            popup_page = FakePopupPage("https://example.com/popup")
            locator = FakePopupLocator()
            executor = ClickExecutor(locator)
            executor.arm_modal_guard()
            self.assertTrue(executor.modal_guard_active())
            result = await executor.execute_node(
                popup_page,
                Workflow(id="demo", name="Demo", version="0.1.0", nodes=[]),
                WorkflowNode(id="click", type="web.click", title="点击", params={"target": "编辑", "opensModal": True}),
                {},
            )
            self.assertEqual(result.status, "pass")
            self.assertTrue(locator.clicked)
            self.assertFalse(executor.modal_guard_active())

        asyncio.run(check_click())

    def test_parse_flow_control_nodes(self) -> None:
        workflow = parse_workflow({
            "id": "flow-control",
            "name": "Flow Control",
            "nodes": [
                {"id": "if-visible", "type": "flow.if", "params": {"selector": "#ready"}},
                {"id": "wait-true", "type": "flow.wait", "params": {"seconds": 1}},
                {"id": "else", "type": "flow.else", "params": {}},
                {"id": "wait-false", "type": "flow.wait", "params": {"seconds": 1}},
                {"id": "end-if", "type": "flow.end_if", "params": {}},
                {"id": "loop", "type": "flow.loop", "params": {"times": 2}},
                {"id": "loop-wait", "type": "flow.wait", "params": {"seconds": 1}},
                {"id": "end-loop", "type": "flow.end_loop", "params": {}},
            ],
        })
        self.assertEqual(len(workflow.nodes), 8)
        self.assertEqual(find_else_or_end_if(workflow.nodes, 0), 2)
        self.assertEqual(find_end_if(workflow.nodes, 2), 4)
        self.assertEqual(find_end_loop(workflow.nodes, 5), 7)

    def test_dianxiaomi_edit_category_modal_branch(self) -> None:
        workflow_path = Path(__file__).resolve().parents[1] / "workflows" / "dianxiaomi_ai_workflow.json"
        workflow = parse_workflow(json.loads(workflow_path.read_text()))
        node_ids = [node.id for node in workflow.nodes]
        self.assertLess(node_ids.index("ai-confirm-draft-page"), node_ids.index("scroll-draft-list-bottom"))
        self.assertLess(node_ids.index("scroll-draft-list-bottom"), node_ids.index("scroll-draft-list-top"))
        self.assertLess(node_ids.index("scroll-draft-list-top"), node_ids.index("loop-products"))
        scroll_bottom = workflow.nodes[node_ids.index("scroll-draft-list-bottom")]
        self.assertEqual(scroll_bottom.type, "web.scroll")
        self.assertEqual(scroll_bottom.params["selector"], ".vxe-table--body-wrapper")
        self.assertEqual(scroll_bottom.params["position"], "bottom")
        scroll_top = workflow.nodes[node_ids.index("scroll-draft-list-top")]
        self.assertEqual(scroll_top.type, "web.scroll")
        self.assertEqual(scroll_top.params["position"], "top")

        modal_if_index = node_ids.index("if-edit-category-modal-visible")
        end_if_index = node_ids.index("end-if-category")
        skipped_to_index = find_else_or_end_if(workflow.nodes, modal_if_index) + 1

        modal_if = workflow.nodes[modal_if_index]
        self.assertEqual(modal_if.type, "flow.if")
        self.assertIn("编辑分类", modal_if.params["selector"])
        self.assertIn("跳过", modal_if.params["selector"])
        self.assertIn("去编辑产品", modal_if.params["selector"])
        self.assertEqual(modal_if.params["pageContext"], "pageEditEntry")
        click_category = workflow.nodes[node_ids.index("click-edit-category-in-modal")]
        self.assertIn("button:has-text(\"编辑分类\")", click_category.params["selector"])
        edit_button_if = workflow.nodes[node_ids.index("if-edit-button-visible")]
        self.assertIn(":nth-match", edit_button_if.params["selector"])
        self.assertIn("${loop-products.index}", edit_button_if.params["selector"])
        self.assertTrue(edit_button_if.params["scrollBeforeLocate"])
        self.assertEqual(edit_button_if.params["scrollBeforeLocateContainer"], ".vxe-table--body-wrapper")
        remark_node = workflow.nodes[node_ids.index("extract-first-product-remark")]
        self.assertEqual(remark_node.type, "web.extract")
        self.assertEqual(remark_node.params["target"], "第${loop-products.index}条商品备注")
        self.assertIn(":nth-match", remark_node.params["selector"])
        self.assertIn("${loop-products.index}", remark_node.params["selector"])
        self.assertTrue(remark_node.params["scrollBeforeLocate"])
        self.assertEqual(remark_node.params["scrollBeforeLocateContainer"], ".vxe-table--body-wrapper")
        self.assertLess(node_ids.index("if-edit-button-visible"), node_ids.index("extract-first-product-remark"))
        self.assertLess(node_ids.index("extract-first-product-remark"), node_ids.index("click-first-edit"))
        self.assertEqual(remark_node.params["saveAs"], "productRemark.raw")
        self.assertEqual(remark_node.params["groupSaveAs"]["type"], "productRemark.type")
        self.assertEqual(remark_node.params["groupSaveAs"]["costPrice"], "productRemark.costPrice")
        self.assertEqual(remark_node.params["groupSaveAs"]["price"], "productRemark.price")
        self.assertEqual(remark_node.params["groupSaveAs"]["weight"], "productRemark.weight")
        self.assertEqual(remark_node.params["groupSaveAs"]["weightText"], "productRemark.weightText")
        click_first_edit = workflow.nodes[node_ids.index("click-first-edit")]
        self.assertIn(":nth-match", click_first_edit.params["selector"])
        self.assertIn("${loop-products.index}", click_first_edit.params["selector"])
        self.assertTrue(click_first_edit.params["scrollBeforeLocate"])
        self.assertEqual(click_first_edit.params["scrollBeforeLocateContainer"], ".vxe-table--body-wrapper")
        title_ai = workflow.nodes[node_ids.index("ai-title-translation")]
        self.assertIn("title", title_ai.params["expectedFormat"])
        self.assertTrue(title_ai.params["rejectCjkTitle"])
        self.assertIn("不能包含任何中文", title_ai.params["prompt"])
        translated_title = workflow.nodes[node_ids.index("write-translated-title")]
        self.assertEqual(translated_title.params["value"], "${ai-title-translation.title}")
        self.assertTrue(translated_title.params["rejectCjk"])
        self.assertTrue(translated_title.params["commitInput"])
        self.assertTrue(translated_title.params["verifyInputValue"])
        custom_attr_if = workflow.nodes[node_ids.index("if-custom-attr-batch-operate-visible")]
        self.assertEqual(custom_attr_if.type, "flow.if")
        self.assertIn("自定义属性", custom_attr_if.params["selector"])
        custom_attr_hover = workflow.nodes[node_ids.index("hover-custom-attr-batch-operate-button")]
        self.assertEqual(custom_attr_hover.type, "web.hover")
        self.assertTrue(custom_attr_hover.params["noScroll"])
        self.assertIn("批量操作", custom_attr_hover.params["selector"])
        self.assertIn("span.link.flex-y-center.inline-flex", custom_attr_hover.params["selector"])
        self.assertIn("i.attach-icons", custom_attr_hover.params["selector"])
        custom_attr_click = workflow.nodes[node_ids.index("click-custom-attr-delete-all-menu")]
        self.assertEqual(custom_attr_click.type, "web.click")
        self.assertIn("删除全部", custom_attr_click.params["selector"])
        self.assertTrue(custom_attr_click.params["opensModal"])
        custom_attr_confirm_if = workflow.nodes[node_ids.index("if-delete-all-custom-attr-confirm-visible")]
        self.assertEqual(custom_attr_confirm_if.type, "flow.if")
        self.assertIn("确定删除全部自定义属性吗", custom_attr_confirm_if.params["selector"])
        self.assertIn(".ant-modal-confirm-content", custom_attr_confirm_if.params["selector"])
        custom_attr_confirm_click = workflow.nodes[node_ids.index("click-confirm-delete-all-custom-attr")]
        self.assertEqual(custom_attr_confirm_click.type, "web.click")
        self.assertIn(".ant-modal-confirm-btns button.ant-btn-primary", custom_attr_confirm_click.params["selector"])
        self.assertIn("确 定", custom_attr_confirm_click.params["selector"])
        self.assertNotIn("click-marketing-1-1-add-image-box", node_ids)
        self.assertNotIn("click-quote-product-image-1-1", node_ids)
        self.assertNotIn("hover-marketing-3-4-scene", node_ids)
        hover_menu = workflow.nodes[node_ids.index("hover-marketing-1-1-add-image-box-check-menu")]
        self.assertEqual(hover_menu.type, "web.hover")
        self.assertTrue(hover_menu.params["noScroll"])
        self.assertIn("img[src*=\"addImg\"]", hover_menu.params["selector"])
        medium_stretch = workflow.nodes[node_ids.index("select-medium-stretch")]
        self.assertEqual(medium_stretch.params["state"], "visible")
        self.assertIn(":visible", medium_stretch.params["selector"])
        self.assertIn("Medium Strecth", medium_stretch.params["selector"])
        self.assertNotIn("pressBeforeClick", medium_stretch.params)
        self.assertNotIn("skipClickAfterPress", medium_stretch.params)
        self.assertIn("ant-select-dropdown", medium_stretch.params["waitAfterClickHidden"])
        self.assertIn("面料弹性", medium_stretch.params["waitAfterClickVisible"])
        self.assertLess(node_ids.index("select-medium-stretch"), node_ids.index("hover-marketing-1-1-add-image-box-check-menu"))
        material_add_row_2 = workflow.nodes[node_ids.index("click-material-add-row-2")]
        material_add_row_3 = workflow.nodes[node_ids.index("click-material-add-row-3")]
        self.assertIn("Material", material_add_row_2.params["selector"])
        self.assertIn("icon_add", material_add_row_2.params["selector"])
        self.assertEqual(material_add_row_2.params["selector"], material_add_row_3.params["selector"])
        self.assertEqual(workflow.nodes[node_ids.index("select-material-row-1-cotton")].params["target"], "COTTON(COTTON)")
        self.assertEqual(workflow.nodes[node_ids.index("input-material-row-1-percent")].params["value"], "40")
        self.assertEqual(workflow.nodes[node_ids.index("select-material-row-2-polyester")].params["target"], "POLYESTER(聚酯纤维)")
        self.assertEqual(workflow.nodes[node_ids.index("input-material-row-2-percent")].params["value"], "40")
        material_acrylic_search = workflow.nodes[node_ids.index("input-material-row-3-search-acrylic")]
        self.assertEqual(material_acrylic_search.params["value"], "Acrylic")
        self.assertIn("ant-select-open", material_acrylic_search.params["selector"])
        material_acrylic = workflow.nodes[node_ids.index("select-material-row-3-acrylic")]
        self.assertEqual(material_acrylic.params["target"], "Acrylic(腈纶)")
        self.assertEqual(workflow.nodes[node_ids.index("input-material-row-3-percent")].params["value"], "20")
        style_node = workflow.nodes[node_ids.index("input-style-classic-style")]
        self.assertEqual(style_node.type, "web.input")
        self.assertEqual(style_node.params["value"], "经典(Classic Style)")
        self.assertIn("Style", style_node.params["selector"])
        style_select = workflow.nodes[node_ids.index("select-style-classic-style")]
        self.assertEqual(style_select.type, "web.click")
        self.assertEqual(style_select.params["target"], "经典(Classic Style)")
        self.assertIn("Classic Style", style_select.params["selector"])
        self.assertIn("ant-select-dropdown", style_select.params["waitAfterClickHidden"])
        silhouette_search = workflow.nodes[node_ids.index("input-silhouette-search-loose")]
        self.assertEqual(silhouette_search.params["value"], "Loose")
        self.assertIn("ant-select-open", silhouette_search.params["selector"])
        self.assertEqual(workflow.nodes[node_ids.index("select-silhouette-loose")].params["target"], "宽松(Loose)")
        season_search = workflow.nodes[node_ids.index("input-season-search-all-season")]
        self.assertEqual(season_search.params["value"], "All Season")
        self.assertEqual(workflow.nodes[node_ids.index("select-season-all-season")].params["target"], "全季节(All Season)")
        age_search = workflow.nodes[node_ids.index("input-age-search-junior")]
        self.assertEqual(age_search.params["value"], "Junior")
        self.assertEqual(workflow.nodes[node_ids.index("select-age-junior")].params["target"], "<24岁(Junior)")
        fabric_type = workflow.nodes[node_ids.index("input-fabric-type-cotton-blend")]
        self.assertEqual(fabric_type.type, "web.input")
        self.assertEqual(fabric_type.params["value"], "cotton blend")
        fabric_type_select = workflow.nodes[node_ids.index("select-fabric-type-cotton-blend")]
        self.assertEqual(fabric_type_select.type, "web.click")
        self.assertEqual(fabric_type_select.params["target"], "棉类混纺(cotton blend)")
        self.assertIn("cotton blend", fabric_type_select.params["selector"])
        self.assertIn("ant-select-dropdown", fabric_type_select.params["waitAfterClickHidden"])
        craft_search = workflow.nodes[node_ids.index("input-craft-of-weaving-search-tatting")]
        self.assertEqual(craft_search.params["value"], "Tatting")
        self.assertEqual(workflow.nodes[node_ids.index("select-craft-of-weaving-tatting")].params["target"], "梭织(Tatting)")
        chemical_search = workflow.nodes[node_ids.index("input-high-concerned-chemical-search-none")]
        self.assertEqual(chemical_search.params["value"], "None")
        self.assertEqual(workflow.nodes[node_ids.index("select-high-concerned-chemical-none")].params["target"], "天然未处理(None)")
        self.assertLess(node_ids.index("click-material-add-row-2"), node_ids.index("click-material-add-row-3"))
        self.assertLess(node_ids.index("click-material-add-row-3"), node_ids.index("click-material-row-1-dropdown"))
        self.assertLess(node_ids.index("click-material-row-3-dropdown"), node_ids.index("input-material-row-3-search-acrylic"))
        self.assertLess(node_ids.index("input-material-row-3-search-acrylic"), node_ids.index("select-material-row-3-acrylic"))
        self.assertLess(node_ids.index("select-cn-province-guangdong"), node_ids.index("click-material-add-row-2"))
        self.assertLess(node_ids.index("input-material-row-3-percent"), node_ids.index("input-style-classic-style"))
        self.assertLess(node_ids.index("input-style-classic-style"), node_ids.index("select-style-classic-style"))
        self.assertLess(node_ids.index("select-style-classic-style"), node_ids.index("click-silhouette-dropdown"))
        self.assertLess(node_ids.index("click-silhouette-dropdown"), node_ids.index("input-silhouette-search-loose"))
        self.assertLess(node_ids.index("input-silhouette-search-loose"), node_ids.index("select-silhouette-loose"))
        self.assertLess(node_ids.index("select-silhouette-loose"), node_ids.index("click-season-dropdown"))
        self.assertLess(node_ids.index("select-season-all-season"), node_ids.index("click-age-dropdown"))
        self.assertLess(node_ids.index("select-age-junior"), node_ids.index("input-fabric-type-cotton-blend"))
        self.assertLess(node_ids.index("input-fabric-type-cotton-blend"), node_ids.index("select-fabric-type-cotton-blend"))
        self.assertLess(node_ids.index("select-fabric-type-cotton-blend"), node_ids.index("click-craft-of-weaving-dropdown"))
        self.assertLess(node_ids.index("select-craft-of-weaving-tatting"), node_ids.index("click-high-concerned-chemical-dropdown"))
        self.assertLess(node_ids.index("select-high-concerned-chemical-none"), node_ids.index("click-marketing-1-1-delete-button"))
        self.assertLess(node_ids.index("input-material-row-3-percent"), node_ids.index("click-marketing-1-1-delete-button"))
        fit_type_open = workflow.nodes[node_ids.index("click-fit-type-dropdown")]
        self.assertEqual(fit_type_open.type, "web.click")
        self.assertEqual(fit_type_open.params["target"], "版型(Fit Type)")
        self.assertIn("Fit Type", fit_type_open.params["selector"])
        for dropdown_id in [
            "click-elasticity-dropdown",
            "click-fit-type-dropdown",
            "click-brand-dropdown",
            "click-origin-dropdown",
            "click-cn-province-dropdown",
            "click-silhouette-dropdown",
            "click-season-dropdown",
            "click-age-dropdown",
            "click-craft-of-weaving-dropdown",
            "click-high-concerned-chemical-dropdown",
        ]:
            dropdown_selector = workflow.nodes[node_ids.index(dropdown_id)].params["selector"]
            self.assertIn("ant-form-item-label", dropdown_selector)
            self.assertNotIn(".ant-row:has-text", dropdown_selector)
            self.assertNotIn("tr:has-text", dropdown_selector)
        fit_type_select = workflow.nodes[node_ids.index("select-regular-fit")]
        self.assertEqual(fit_type_select.type, "web.click")
        self.assertEqual(fit_type_select.params["target"], "合体(Regular Fit)")
        self.assertIn("Regular Fit", fit_type_select.params["selector"])
        brand_select = workflow.nodes[node_ids.index("select-brand-dancing-jl-ants")]
        self.assertEqual(brand_select.params["target"], "Dancing JL Ants")
        self.assertIn("Dancing JL Ants", brand_select.params["selector"])
        origin_select = workflow.nodes[node_ids.index("select-origin-mainland-china")]
        self.assertEqual(origin_select.params["target"], "中国大陆(Origin)(Mainland China)")
        self.assertIn("Mainland China", origin_select.params["selector"])
        province_select = workflow.nodes[node_ids.index("select-cn-province-guangdong")]
        self.assertEqual(province_select.params["target"], "广东(Guangdong)")
        self.assertIn("Guangdong", province_select.params["selector"])
        self.assertLess(node_ids.index("select-medium-stretch"), node_ids.index("click-fit-type-dropdown"))
        self.assertLess(node_ids.index("click-fit-type-dropdown"), node_ids.index("select-regular-fit"))
        self.assertLess(node_ids.index("select-regular-fit"), node_ids.index("click-brand-dropdown"))
        self.assertLess(node_ids.index("click-brand-dropdown"), node_ids.index("select-brand-dancing-jl-ants"))
        self.assertLess(node_ids.index("select-brand-dancing-jl-ants"), node_ids.index("click-origin-dropdown"))
        self.assertLess(node_ids.index("click-origin-dropdown"), node_ids.index("select-origin-mainland-china"))
        self.assertLess(node_ids.index("select-origin-mainland-china"), node_ids.index("click-cn-province-dropdown"))
        self.assertLess(node_ids.index("click-cn-province-dropdown"), node_ids.index("select-cn-province-guangdong"))
        self.assertLess(node_ids.index("select-cn-province-guangdong"), node_ids.index("click-material-add-row-2"))
        self.assertLess(node_ids.index("input-material-row-3-percent"), node_ids.index("click-marketing-1-1-delete-button"))
        quote_menu = workflow.nodes[node_ids.index("click-marketing-1-1-quote-menu-item")]
        self.assertEqual(quote_menu.type, "web.click")
        self.assertIn("引用产品图片", quote_menu.params["selector"])
        self.assertTrue(quote_menu.params["opensModal"])
        self.assertLess(node_ids.index("hover-marketing-1-1-add-image-box-check-menu"), node_ids.index("click-marketing-1-1-quote-menu-item"))
        first_image = workflow.nodes[node_ids.index("click-marketing-1-1-first-image")]
        self.assertEqual(first_image.type, "web.click")
        self.assertIn(".ant-modal:visible", first_image.params["selector"])
        confirm_selection = workflow.nodes[node_ids.index("click-marketing-1-1-confirm-selection")]
        self.assertEqual(confirm_selection.type, "web.click")
        self.assertIn("button:has-text(\"选择\")", confirm_selection.params["selector"])
        delete_button = workflow.nodes[node_ids.index("click-marketing-1-1-delete-button")]
        self.assertEqual(delete_button.type, "web.click")
        self.assertIn(".icon_delete.filter-sortable", delete_button.params["selector"])
        self.assertIn("1:1白底图", delete_button.params["selector"])
        self.assertTrue(delete_button.params["noScroll"])
        delete_3_4 = workflow.nodes[node_ids.index("click-marketing-3-4-delete-button")]
        self.assertEqual(delete_3_4.type, "web.click")
        self.assertIn(".icon_delete.filter-sortable", delete_3_4.params["selector"])
        self.assertIn("3:4场景图", delete_3_4.params["selector"])
        self.assertTrue(delete_3_4.params["noScroll"])
        operate_hover = workflow.nodes[node_ids.index("hover-marketing-1-1-operate-button-check-menu")]
        self.assertEqual(operate_hover.type, "web.hover")
        self.assertIn(".icon-operate.filter-sortable", operate_hover.params["selector"])
        self.assertIn(".flex-y-center:has(a.icon-operate.filter-sortable)", operate_hover.params["selector"])
        self.assertIn("1:1白底图", operate_hover.params["selector"])
        self.assertTrue(operate_hover.params["noScroll"])
        resize_menu = workflow.nodes[node_ids.index("click-marketing-1-1-resize-menu-item")]
        self.assertEqual(resize_menu.type, "web.click")
        self.assertIn("修改图片尺寸", resize_menu.params["selector"])
        self.assertIn(".ant-dropdown:visible", resize_menu.params["selector"])
        self.assertIn("[role=menu]:visible", resize_menu.params["selector"])
        self.assertTrue(resize_menu.params["opensModal"])
        self.assertNotIn('button:has-text("修改图片尺寸")', resize_menu.params["selector"])
        self.assertNotIn('li:has-text("修改图片尺寸")', resize_menu.params["selector"])
        resize_width = workflow.nodes[node_ids.index("input-resize-width-800")]
        self.assertEqual(resize_width.type, "web.input")
        self.assertEqual(resize_width.params["value"], "800")
        self.assertIn('input[name="valueW"]', resize_width.params["selector"])
        resize_ratio = workflow.nodes[node_ids.index("select-resize-ratio-1-1")]
        self.assertIn("1:1", resize_ratio.params["selector"])
        self.assertEqual(resize_ratio.params["state"], "visible")
        self.assertIn('[title="1:1"]', resize_ratio.params["selector"])
        self.assertIn(":text-is(\"1:1\")", resize_ratio.params["selector"])
        wait_ratio_selected = workflow.nodes[node_ids.index("wait-resize-ratio-1-1-selected")]
        self.assertEqual(wait_ratio_selected.type, "web.wait_for")
        self.assertIn("ant-select-selection-item", wait_ratio_selected.params["selector"])
        generate_jpg = workflow.nodes[node_ids.index("click-generate-jpg-image")]
        self.assertEqual(generate_jpg.type, "web.click")
        self.assertIn("生成JPG图片", generate_jpg.params["selector"])
        wait_after_jpg = workflow.nodes[node_ids.index("wait-after-generate-jpg")]
        self.assertEqual(wait_after_jpg.type, "flow.wait")
        self.assertEqual(wait_after_jpg.params["seconds"], 5)
        wait_resize_closed = workflow.nodes[node_ids.index("wait-resize-image-modal-closed")]
        self.assertEqual(wait_resize_closed.type, "web.wait_for")
        self.assertEqual(wait_resize_closed.params["state"], "hidden")
        self.assertIn("批量改图片尺寸", wait_resize_closed.params["selector"])
        generate_marketing = workflow.nodes[node_ids.index("click-generate-marketing-images")]
        self.assertEqual(generate_marketing.type, "web.click")
        self.assertIn("一键生成", generate_marketing.params["selector"])
        batch_retail = workflow.nodes[node_ids.index("input-sku-batch-retail-price")]
        self.assertEqual(batch_retail.type, "web.input")
        self.assertEqual(batch_retail.params["valueExpression"], "(${productRemark.costPrice} + (${productRemark.weight} * 0.081 + 19)) * 1.2 / 6.3 / 0.92 / 0.95 * 2")
        self.assertIn('input[placeholder="零售价"]', batch_retail.params["selector"])
        self.assertIn('[class*="w-70"]', batch_retail.params["selector"])
        self.assertIn("th:nth-child(3)", batch_retail.params["selector"])
        self.assertEqual(batch_retail.params["state"], "visible")
        self.assertTrue(batch_retail.params["commitInput"])
        self.assertTrue(batch_retail.params["verifyInputValue"])
        batch_goods_value = workflow.nodes[node_ids.index("input-sku-batch-goods-value")]
        self.assertEqual(batch_goods_value.params["valueExpression"], "${productRemark.costPrice} * 1.5")
        self.assertIn('input[placeholder="货值"]', batch_goods_value.params["selector"])
        self.assertIn("th:nth-child(4)", batch_goods_value.params["selector"])
        sku_batch_input_ids = [
            "input-sku-batch-retail-price",
            "input-sku-batch-goods-value",
            "input-sku-batch-stock",
            "input-sku-batch-weight",
            "input-sku-batch-package-length",
            "input-sku-batch-package-width",
            "input-sku-batch-package-height",
        ]
        for sku_input_id in sku_batch_input_ids:
            sku_input = workflow.nodes[node_ids.index(sku_input_id)]
            self.assertIn("table.myj-table:visible", sku_input.params["selector"])
            self.assertEqual(sku_input.params["state"], "visible")
            self.assertTrue(sku_input.params["commitInput"])
            self.assertTrue(sku_input.params["verifyInputValue"])
        self.assertEqual(workflow.nodes[node_ids.index("input-sku-batch-stock")].params["value"], "1000")
        self.assertIn("th:nth-child(6)", workflow.nodes[node_ids.index("input-sku-batch-stock")].params["selector"])
        batch_weight = workflow.nodes[node_ids.index("input-sku-batch-weight")]
        self.assertEqual(batch_weight.params["valueExpression"], "${productRemark.weight} / 1000")
        self.assertIn("th:nth-child(7)", batch_weight.params["selector"])
        self.assertEqual(workflow.nodes[node_ids.index("input-sku-batch-package-length")].params["value"], "35")
        self.assertIn("th:nth-child(8)", workflow.nodes[node_ids.index("input-sku-batch-package-length")].params["selector"])
        self.assertEqual(workflow.nodes[node_ids.index("input-sku-batch-package-width")].params["value"], "30")
        self.assertIn("th:nth-child(8)", workflow.nodes[node_ids.index("input-sku-batch-package-width")].params["selector"])
        self.assertEqual(workflow.nodes[node_ids.index("input-sku-batch-package-height")].params["value"], "4")
        self.assertIn("th:nth-child(8)", workflow.nodes[node_ids.index("input-sku-batch-package-height")].params["selector"])
        origin_country = workflow.nodes[node_ids.index("select-sku-origin-country-yes")]
        self.assertEqual(origin_country.type, "web.select")
        self.assertEqual(origin_country.params["value"], "1")
        self.assertIn("table.myj-table:visible thead", origin_country.params["selector"])
        self.assertIn("select:has", origin_country.params["selector"])
        self.assertNotIn("ant-form-item-label", origin_country.params["selector"])
        self.assertNotIn("click-origin-country-dropdown", node_ids)
        self.assertNotIn("select-origin-country-yes", node_ids)
        sku_batch_check = workflow.nodes[node_ids.index("check-sku-batch-inputs-before-fill")]
        self.assertEqual(sku_batch_check.type, "web.wait_for")
        self.assertNotIn("stopAfter", sku_batch_check.params)
        self.assertEqual(len(sku_batch_check.params["inputValues"]), 7)
        self.assertEqual(sku_batch_check.params["inputValues"][0]["valueExpression"], "(${productRemark.costPrice} + (${productRemark.weight} * 0.081 + 19)) * 1.2 / 6.3 / 0.92 / 0.95 * 2")
        self.assertIn("th:nth-child(3)", sku_batch_check.params["inputValues"][0]["selector"])
        self.assertEqual(sku_batch_check.params["inputValues"][1]["valueExpression"], "${productRemark.costPrice} * 1.5")
        self.assertIn("th:nth-child(4)", sku_batch_check.params["inputValues"][1]["selector"])
        self.assertIn("th:nth-child(6)", sku_batch_check.params["inputValues"][2]["selector"])
        self.assertEqual(sku_batch_check.params["inputValues"][3]["valueExpression"], "${productRemark.weight} / 1000")
        self.assertIn("th:nth-child(7)", sku_batch_check.params["inputValues"][3]["selector"])
        for input_check in sku_batch_check.params["inputValues"]:
            self.assertIn("table.myj-table:visible", input_check["selector"])
        sku_batch_fill = workflow.nodes[node_ids.index("click-sku-batch-fill")]
        self.assertEqual(sku_batch_fill.type, "web.click")
        self.assertFalse(sku_batch_fill.disabled)
        self.assertIn("批量填充", sku_batch_fill.params["selector"])
        self.assertIn(":visible:not([disabled])", sku_batch_fill.params["selector"])
        logistics_batch = workflow.nodes[node_ids.index("click-logistics-attribute-batch")]
        self.assertEqual(logistics_batch.type, "web.click")
        self.assertFalse(logistics_batch.disabled)
        self.assertIn("物流属性", logistics_batch.params["selector"])
        self.assertIn("批量", logistics_batch.params["selector"])
        self.assertTrue(logistics_batch.params["opensModal"])
        logistics_modal = workflow.nodes[node_ids.index("wait-logistics-attribute-modal")]
        self.assertEqual(logistics_modal.type, "web.wait_for")
        self.assertIn("物流属性", logistics_modal.params["selector"])
        common_goods_if = workflow.nodes[node_ids.index("if-common-goods-unchecked")]
        self.assertEqual(common_goods_if.type, "flow.if")
        self.assertIn("普货", common_goods_if.params["selector"])
        self.assertIn("not(:has(.ant-checkbox-checked))", common_goods_if.params["selector"])
        common_goods_click = workflow.nodes[node_ids.index("click-common-goods-checkbox")]
        self.assertEqual(common_goods_click.type, "web.click")
        self.assertIn("普货", common_goods_click.params["selector"])
        logistics_confirm = workflow.nodes[node_ids.index("click-logistics-attribute-confirm")]
        self.assertEqual(logistics_confirm.type, "web.click")
        self.assertIn("物流属性", logistics_confirm.params["selector"])
        self.assertIn("button.ant-btn-primary", logistics_confirm.params["selector"])
        clean_pc_description = workflow.nodes[node_ids.index("clean-pc-description-keep-images")]
        self.assertEqual(clean_pc_description.type, "web.input")
        self.assertTrue(clean_pc_description.params["richTextKeepImagesOnly"])
        self.assertEqual(clean_pc_description.params["richTextPrefixFromProductType"], "${productRemark.type}")
        self.assertTrue(clean_pc_description.params["allowEmpty"])
        self.assertEqual(clean_pc_description.params["state"], "attached")
        self.assertIn("textarea[id^=\"ckeditor\"]", clean_pc_description.params["selector"])
        self.assertIn("textarea#ckeditor30", clean_pc_description.params["selector"])
        self.assertNotIn("click-pc-description-reference-template", node_ids)
        self.assertNotIn("wait-product-info-module-modal", node_ids)
        self.assertNotIn("click-custom-template-tab", node_ids)
        self.assertNotIn("if-pc-template-type-unchecked", node_ids)
        self.assertNotIn("click-pc-template-type-checkbox", node_ids)
        self.assertNotIn("end-if-pc-template-type-unchecked", node_ids)
        self.assertNotIn("click-product-info-module-confirm", node_ids)
        wireless_none = workflow.nodes[node_ids.index("click-wireless-description-none")]
        self.assertEqual(wireless_none.type, "web.click")
        self.assertIn("不填写无线端描述", wireless_none.params["selector"])
        wireless_new_editor = workflow.nodes[node_ids.index("click-wireless-description-new-editor")]
        self.assertEqual(wireless_new_editor.type, "web.click")
        self.assertTrue(wireless_new_editor.params["opensModal"])
        self.assertIn("使用新版编辑器", wireless_new_editor.params["selector"])
        wireless_editor = workflow.nodes[node_ids.index("wait-wireless-new-editor")]
        self.assertEqual(wireless_editor.type, "web.wait_for")
        self.assertIn("新版编辑器", wireless_editor.params["selector"])
        wireless_generate = workflow.nodes[node_ids.index("click-wireless-generate-from-pc")]
        self.assertEqual(wireless_generate.type, "web.click")
        self.assertTrue(wireless_generate.params["opensModal"])
        self.assertIn("根据PC端描述一键生成", wireless_generate.params["selector"])
        self.assertIn("无线端内容", wireless_generate.params["waitAfterClickVisible"])
        wireless_generate_confirm = workflow.nodes[node_ids.index("click-wireless-generate-confirm")]
        self.assertEqual(wireless_generate_confirm.type, "web.click")
        self.assertIn("提示", wireless_generate_confirm.params["selector"])
        self.assertIn("无线端内容", wireless_generate_confirm.params["selector"])
        self.assertIn(".ant-modal-confirm-title", wireless_generate_confirm.params["selector"])
        self.assertIn(".ant-modal-confirm-btns", wireless_generate_confirm.params["selector"])
        self.assertIn("确 定", wireless_generate_confirm.params["selector"])
        self.assertIn("无线端内容", wireless_generate_confirm.params["waitAfterClickHidden"])
        wireless_generate_wait = workflow.nodes[node_ids.index("wait-after-wireless-generate")]
        self.assertEqual(wireless_generate_wait.type, "flow.wait")
        self.assertEqual(wireless_generate_wait.params["seconds"], 2)
        wireless_editor_save = workflow.nodes[node_ids.index("click-wireless-new-editor-save")]
        self.assertEqual(wireless_editor_save.type, "web.click")
        self.assertIn("保存", wireless_editor_save.params["selector"])
        self.assertIn(".ant-modal-header", wireless_editor_save.params["selector"])
        self.assertIn(".title-right", wireless_editor_save.params["selector"])
        self.assertIn("btn-orange", wireless_editor_save.params["selector"])
        self.assertIn(".ant-modal-header", wireless_editor_save.params["waitAfterClickHidden"])
        self.assertIn("btn-orange", wireless_editor_save.params["waitAfterClickHidden"])
        package_weight = workflow.nodes[node_ids.index("input-package-info-weight")]
        self.assertEqual(package_weight.type, "web.input")
        self.assertEqual(package_weight.params["valueExpression"], "${productRemark.weight} / 1000")
        self.assertIn("包装后重量", package_weight.params["selector"])
        self.assertTrue(package_weight.params["commitInput"])
        self.assertTrue(package_weight.params["verifyInputValue"])
        package_length = workflow.nodes[node_ids.index("input-package-info-length")]
        self.assertEqual(package_length.params["value"], "35")
        self.assertIn("包装后尺寸", package_length.params["selector"])
        self.assertIn(":nth-match", package_length.params["selector"])
        package_width = workflow.nodes[node_ids.index("input-package-info-width")]
        self.assertEqual(package_width.params["value"], "30")
        self.assertIn("包装后尺寸", package_width.params["selector"])
        self.assertIn(":nth-match", package_width.params["selector"])
        package_height = workflow.nodes[node_ids.index("input-package-info-height")]
        self.assertEqual(package_height.params["value"], "4")
        self.assertIn("包装后尺寸", package_height.params["selector"])
        self.assertIn(":nth-match", package_height.params["selector"])
        freight_dropdown = workflow.nodes[node_ids.index("click-freight-template-dropdown")]
        self.assertEqual(freight_dropdown.type, "web.click")
        self.assertIn("运费模板", freight_dropdown.params["selector"])
        freight_template = workflow.nodes[node_ids.index("select-freight-template-by-weight")]
        self.assertEqual(freight_template.type, "web.click")
        self.assertIn("${freightTemplateByWeight(productRemark.weight)}", freight_template.params["selector"])
        freight_template_selected = workflow.nodes[node_ids.index("wait-freight-template-selected")]
        self.assertEqual(freight_template_selected.type, "web.wait_for")
        self.assertIn("${freightTemplateByWeight(productRemark.weight)}", freight_template_selected.params["selector"])
        service_dropdown = workflow.nodes[node_ids.index("click-service-template-dropdown")]
        self.assertEqual(service_dropdown.type, "web.click")
        self.assertIn("服务模板", service_dropdown.params["selector"])
        service_template = workflow.nodes[node_ids.index("select-service-template-new-sellers")]
        self.assertEqual(service_template.type, "web.click")
        self.assertIn("Service Template for New Sellers", service_template.params["selector"])
        service_template_selected = workflow.nodes[node_ids.index("wait-service-template-selected")]
        self.assertEqual(service_template_selected.type, "web.wait_for")
        self.assertIn("Service Template for New Sellers", service_template_selected.params["selector"])
        price_excluding_tax = workflow.nodes[node_ids.index("click-price-excluding-tax")]
        self.assertEqual(price_excluding_tax.type, "web.click")
        self.assertIn("报价是否含关税", price_excluding_tax.params["selector"])
        self.assertIn("不含关税报价", price_excluding_tax.params["selector"])
        eu_responsible_dropdown = workflow.nodes[node_ids.index("click-eu-responsible-person-dropdown")]
        self.assertEqual(eu_responsible_dropdown.type, "web.click")
        self.assertIn("欧盟责任人", eu_responsible_dropdown.params["selector"])
        eu_responsible = workflow.nodes[node_ids.index("select-eu-responsible-person-discount-industry")]
        self.assertEqual(eu_responsible.type, "web.click")
        self.assertIn("DISCOUNT INDUSTRY", eu_responsible.params["selector"])
        eu_responsible_selected = workflow.nodes[node_ids.index("wait-eu-responsible-person-selected")]
        self.assertEqual(eu_responsible_selected.type, "web.wait_for")
        self.assertIn("DISCOUNT INDUSTRY", eu_responsible_selected.params["selector"])
        brand_manufacturer_dropdown = workflow.nodes[node_ids.index("click-brand-manufacturer-dropdown")]
        self.assertEqual(brand_manufacturer_dropdown.type, "web.click")
        self.assertIn("品牌制造商", brand_manufacturer_dropdown.params["selector"])
        brand_manufacturer = workflow.nodes[node_ids.index("select-brand-manufacturer-baiti")]
        self.assertEqual(brand_manufacturer.type, "web.click")
        self.assertIn("Lu'an Baiti Translation Co., Ltd", brand_manufacturer.params["selector"])
        brand_manufacturer_selected = workflow.nodes[node_ids.index("wait-brand-manufacturer-selected")]
        self.assertEqual(brand_manufacturer_selected.type, "web.wait_for")
        self.assertIn("Lu'an Baiti Translation Co., Ltd", brand_manufacturer_selected.params["selector"])
        ai_confirm_before_save = workflow.nodes[node_ids.index("ai-confirm-before-save")]
        self.assertFalse(ai_confirm_before_save.disabled)
        detail_save = workflow.nodes[node_ids.index("click-detail-save")]
        self.assertEqual(detail_save.type, "web.click")
        self.assertIn("保存", detail_save.params["selector"])
        wait_after_detail_save = workflow.nodes[node_ids.index("wait-after-detail-save")]
        self.assertEqual(wait_after_detail_save.type, "flow.wait")
        self.assertEqual(wait_after_detail_save.params["seconds"], 2)
        close_detail_tab = workflow.nodes[node_ids.index("close-detail-tab-back-to-list")]
        self.assertEqual(close_detail_tab.type, "web.close_tab")
        self.assertEqual(close_detail_tab.params["switchToPageContext"], "page2")
        for removed_row_input_id in [
            "input-sku-row-retail-price-all",
            "input-sku-row-goods-value-all",
            "input-sku-row-stock-all",
            "input-sku-row-weight-all",
            "input-sku-row-package-length-all",
            "input-sku-row-package-width-all",
            "input-sku-row-package-height-all",
        ]:
            self.assertNotIn(removed_row_input_id, node_ids)
        for node in workflow.nodes:
            selector = str(node.params.get("selector") or "")
            if node.id.startswith("input-sku-"):
                self.assertNotIn("tbody tr", selector)
        self.assertLess(node_ids.index("select-medium-stretch"), node_ids.index("click-marketing-1-1-delete-button"))
        self.assertLess(node_ids.index("click-marketing-1-1-delete-button"), node_ids.index("click-marketing-3-4-delete-button"))
        self.assertLess(node_ids.index("click-marketing-3-4-delete-button"), node_ids.index("hover-marketing-1-1-add-image-box-check-menu"))
        self.assertLess(node_ids.index("hover-marketing-1-1-add-image-box-check-menu"), node_ids.index("click-marketing-1-1-quote-menu-item"))
        self.assertLess(node_ids.index("click-marketing-1-1-quote-menu-item"), node_ids.index("click-marketing-1-1-first-image"))
        self.assertLess(node_ids.index("click-marketing-1-1-first-image"), node_ids.index("click-marketing-1-1-confirm-selection"))
        self.assertLess(node_ids.index("click-marketing-1-1-confirm-selection"), node_ids.index("hover-marketing-1-1-operate-button-check-menu"))
        self.assertLess(node_ids.index("hover-marketing-1-1-operate-button-check-menu"), node_ids.index("click-marketing-1-1-resize-menu-item"))
        self.assertLess(node_ids.index("click-marketing-1-1-resize-menu-item"), node_ids.index("wait-resize-image-modal"))
        self.assertLess(node_ids.index("wait-resize-image-modal"), node_ids.index("open-resize-mode-select"))
        self.assertLess(node_ids.index("open-resize-mode-select"), node_ids.index("select-custom-ratio-resize"))
        self.assertLess(node_ids.index("select-custom-ratio-resize"), node_ids.index("open-resize-dimension-select"))
        self.assertLess(node_ids.index("open-resize-dimension-select"), node_ids.index("select-image-width"))
        self.assertLess(node_ids.index("select-image-width"), node_ids.index("input-resize-width-800"))
        self.assertLess(node_ids.index("input-resize-width-800"), node_ids.index("open-resize-ratio-select"))
        self.assertLess(node_ids.index("open-resize-ratio-select"), node_ids.index("select-resize-ratio-1-1"))
        self.assertLess(node_ids.index("select-resize-ratio-1-1"), node_ids.index("wait-resize-ratio-1-1-selected"))
        self.assertLess(node_ids.index("wait-resize-ratio-1-1-selected"), node_ids.index("click-generate-jpg-image"))
        self.assertLess(node_ids.index("click-generate-jpg-image"), node_ids.index("wait-after-generate-jpg"))
        self.assertLess(node_ids.index("wait-after-generate-jpg"), node_ids.index("wait-resize-image-modal-closed"))
        self.assertLess(node_ids.index("wait-resize-image-modal-closed"), node_ids.index("click-generate-marketing-images"))
        self.assertLess(node_ids.index("click-generate-marketing-images"), node_ids.index("input-sku-batch-retail-price"))
        self.assertLess(node_ids.index("input-sku-batch-retail-price"), node_ids.index("input-sku-batch-goods-value"))
        self.assertLess(node_ids.index("input-sku-batch-goods-value"), node_ids.index("input-sku-batch-stock"))
        self.assertLess(node_ids.index("input-sku-batch-stock"), node_ids.index("input-sku-batch-weight"))
        self.assertLess(node_ids.index("input-sku-batch-weight"), node_ids.index("input-sku-batch-package-length"))
        self.assertLess(node_ids.index("input-sku-batch-package-length"), node_ids.index("input-sku-batch-package-width"))
        self.assertLess(node_ids.index("input-sku-batch-package-width"), node_ids.index("input-sku-batch-package-height"))
        self.assertLess(node_ids.index("input-sku-batch-package-height"), node_ids.index("select-sku-origin-country-yes"))
        self.assertLess(node_ids.index("select-sku-origin-country-yes"), node_ids.index("check-sku-batch-inputs-before-fill"))
        self.assertLess(node_ids.index("check-sku-batch-inputs-before-fill"), node_ids.index("click-sku-batch-fill"))
        self.assertLess(node_ids.index("click-sku-batch-fill"), node_ids.index("click-logistics-attribute-batch"))
        self.assertLess(node_ids.index("click-logistics-attribute-batch"), node_ids.index("wait-logistics-attribute-modal"))
        self.assertLess(node_ids.index("wait-logistics-attribute-modal"), node_ids.index("if-common-goods-unchecked"))
        self.assertLess(node_ids.index("if-common-goods-unchecked"), node_ids.index("click-common-goods-checkbox"))
        self.assertLess(node_ids.index("click-common-goods-checkbox"), node_ids.index("end-if-common-goods-unchecked"))
        self.assertLess(node_ids.index("end-if-common-goods-unchecked"), node_ids.index("click-logistics-attribute-confirm"))
        self.assertLess(node_ids.index("click-logistics-attribute-confirm"), node_ids.index("clean-pc-description-keep-images"))
        self.assertLess(node_ids.index("clean-pc-description-keep-images"), node_ids.index("click-wireless-description-none"))
        self.assertLess(node_ids.index("click-wireless-description-none"), node_ids.index("click-wireless-description-new-editor"))
        self.assertLess(node_ids.index("click-wireless-description-new-editor"), node_ids.index("wait-wireless-new-editor"))
        self.assertLess(node_ids.index("wait-wireless-new-editor"), node_ids.index("click-wireless-generate-from-pc"))
        self.assertLess(node_ids.index("click-wireless-generate-from-pc"), node_ids.index("click-wireless-generate-confirm"))
        self.assertLess(node_ids.index("click-wireless-generate-confirm"), node_ids.index("wait-after-wireless-generate"))
        self.assertLess(node_ids.index("wait-after-wireless-generate"), node_ids.index("click-wireless-new-editor-save"))
        self.assertLess(node_ids.index("click-wireless-new-editor-save"), node_ids.index("input-package-info-weight"))
        self.assertLess(node_ids.index("input-package-info-weight"), node_ids.index("input-package-info-length"))
        self.assertLess(node_ids.index("input-package-info-length"), node_ids.index("input-package-info-width"))
        self.assertLess(node_ids.index("input-package-info-width"), node_ids.index("input-package-info-height"))
        self.assertLess(node_ids.index("input-package-info-height"), node_ids.index("click-freight-template-dropdown"))
        self.assertLess(node_ids.index("click-freight-template-dropdown"), node_ids.index("select-freight-template-by-weight"))
        self.assertLess(node_ids.index("select-freight-template-by-weight"), node_ids.index("wait-freight-template-selected"))
        self.assertLess(node_ids.index("wait-freight-template-selected"), node_ids.index("click-service-template-dropdown"))
        self.assertLess(node_ids.index("click-service-template-dropdown"), node_ids.index("select-service-template-new-sellers"))
        self.assertLess(node_ids.index("select-service-template-new-sellers"), node_ids.index("wait-service-template-selected"))
        self.assertLess(node_ids.index("wait-service-template-selected"), node_ids.index("click-price-excluding-tax"))
        self.assertLess(node_ids.index("click-price-excluding-tax"), node_ids.index("click-eu-responsible-person-dropdown"))
        self.assertLess(node_ids.index("click-eu-responsible-person-dropdown"), node_ids.index("select-eu-responsible-person-discount-industry"))
        self.assertLess(node_ids.index("select-eu-responsible-person-discount-industry"), node_ids.index("wait-eu-responsible-person-selected"))
        self.assertLess(node_ids.index("wait-eu-responsible-person-selected"), node_ids.index("click-brand-manufacturer-dropdown"))
        self.assertLess(node_ids.index("click-brand-manufacturer-dropdown"), node_ids.index("select-brand-manufacturer-baiti"))
        self.assertLess(node_ids.index("select-brand-manufacturer-baiti"), node_ids.index("wait-brand-manufacturer-selected"))
        self.assertLess(node_ids.index("wait-brand-manufacturer-selected"), node_ids.index("ai-confirm-before-save"))
        self.assertLess(node_ids.index("ai-confirm-before-save"), node_ids.index("click-detail-save"))
        self.assertLess(node_ids.index("click-detail-save"), node_ids.index("wait-after-detail-save"))
        self.assertLess(node_ids.index("wait-after-detail-save"), node_ids.index("close-detail-tab-back-to-list"))
        self.assertLess(node_ids.index("write-translated-title"), node_ids.index("if-custom-attr-batch-operate-visible"))
        self.assertLess(node_ids.index("if-custom-attr-batch-operate-visible"), node_ids.index("hover-custom-attr-batch-operate-button"))
        self.assertLess(node_ids.index("hover-custom-attr-batch-operate-button"), node_ids.index("click-custom-attr-delete-all-menu"))
        self.assertLess(node_ids.index("click-custom-attr-delete-all-menu"), node_ids.index("if-delete-all-custom-attr-confirm-visible"))
        self.assertLess(node_ids.index("if-delete-all-custom-attr-confirm-visible"), node_ids.index("click-confirm-delete-all-custom-attr"))
        self.assertLess(node_ids.index("click-confirm-delete-all-custom-attr"), node_ids.index("end-if-delete-all-custom-attr-confirm-visible"))
        self.assertLess(node_ids.index("end-if-delete-all-custom-attr-confirm-visible"), node_ids.index("end-if-custom-attr-batch-operate-visible"))
        self.assertLess(node_ids.index("end-if-custom-attr-batch-operate-visible"), node_ids.index("click-elasticity-dropdown"))
        self.assertLess(node_ids.index("wait-modify-category-page"), end_if_index)
        self.assertEqual(workflow.nodes[node_ids.index("wait-modify-category-page")].params["urlIncludes"], "/web/modifyCategory")
        self.assertEqual(workflow.nodes[skipped_to_index].id, "wait-detail-page")


class FakeNonstandardModalPage:
    def __init__(self) -> None:
        self.closed = False
        self.escape_pressed = False
        self.keyboard = FakeKeyboard(self)

    def locator(self, selector: str) -> "FakeLocator":
        if selector == MODAL_CONTAINER_SELECTOR:
            return FakeLocator(lambda: False)
        if selector == GLOBAL_MODAL_CLOSE_SELECTOR:
            return FakeLocator(lambda: not self.closed, self.close)
        return FakeLocator(lambda: False)

    def close(self) -> None:
        self.closed = True

    def describe_visible_modals(self) -> list[dict[str, Any]]:
        if self.closed:
            return []
        return [{
            "text": "平台公告",
            "buttonTexts": ["知道了"],
            "hasFormFields": False,
            "hasStructuredContent": False,
            "hasDismissControl": True,
        }]

    def click_modal_close_by_index(self, modal_index: int) -> bool:
        if modal_index != 0 or self.closed:
            return False
        self.close()
        return True

    async def wait_for_timeout(self, timeout_ms: int) -> None:
        return None


class FakeBusinessModalPage(FakeNonstandardModalPage):
    def describe_visible_modals(self) -> list[dict[str, Any]]:
        if self.closed:
            return []
        return [{
            "text": "编辑分类 去编辑产品",
            "buttonTexts": ["编辑分类", "跳过"],
            "hasFormFields": False,
            "hasStructuredContent": False,
            "hasDismissControl": True,
        }]


class FakeKeyboard:
    def __init__(self, page: FakeNonstandardModalPage) -> None:
        self.page = page

    async def press(self, key: str) -> None:
        self.page.escape_pressed = True


class FakeLocator:
    def __init__(self, visible: Any, click_callback: Any | None = None) -> None:
        self.visible = visible
        self.click_callback = click_callback

    @property
    def first(self) -> "FakeLocator":
        return self

    def locator(self, selector: str) -> "FakeLocator":
        return FakeLocator(lambda: False)

    async def count(self) -> int:
        return 1 if self.is_currently_visible() else 0

    async def is_visible(self, timeout: int = 0) -> bool:
        return self.is_currently_visible()

    async def click(self, timeout: int = 0) -> None:
        if self.click_callback:
            self.click_callback()

    def is_currently_visible(self) -> bool:
        return bool(self.visible() if callable(self.visible) else self.visible)


if __name__ == "__main__":
    unittest.main()
