from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .models import RunStep, Workflow, WorkflowNode
from .paths import bundled_root


CAPTCHA_PATTERN = re.compile(
    r"验证码|校验码|图形码|captcha|verify\s*code|verifycode|verification|vcode",
    re.IGNORECASE,
)
CJK_PATTERN = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]"
)

MODAL_CONTAINER_SELECTOR = (
    ".ant-modal:visible, .el-dialog:visible, .modal:visible, "
    ".modal-dialog:visible, .modal-content:visible, [role=dialog]:visible, "
    "[aria-modal='true']:visible, .layui-layer:visible, .ui-dialog:visible, "
    ".bootbox:visible, .jbox:visible, .jBox:visible, "
    "[class*='modal' i]:visible, [class*='dialog' i]:visible, "
    "[class*='popup' i]:visible, [class*='pop' i]:visible, [class*='layer' i]:visible"
)
MODAL_CLOSE_ICON_DOM_SELECTOR = (
    ".ant-modal-close, .el-dialog__headerbtn, .layui-layer-close, "
    ".ui-dialog-titlebar-close, [aria-label='Close'], [aria-label='close'], "
    "[aria-label='关闭'], [title='关闭'], [data-dismiss='modal'], "
    "[data-bs-dismiss='modal'], .close, [class*='close' i]"
)
MODAL_CLOSE_SELECTOR = (
    ".ant-modal-close, .el-dialog__headerbtn, .layui-layer-close, "
    ".ui-dialog-titlebar-close, [aria-label='Close'], [aria-label='close'], "
    "[aria-label='关闭'], [title='关闭'], [data-dismiss='modal'], "
    "[data-bs-dismiss='modal'], .close, [class*='close' i], "
    "button:has-text('关闭'), a:has-text('关闭'), [role=button]:has-text('关闭'), "
    "button:has-text('取消'), a:has-text('取消'), [role=button]:has-text('取消'), "
    "button:has-text('我知道了'), button:has-text('知道了'), "
    "a:has-text('我知道了'), a:has-text('知道了'), "
    "button:has-text('×'), a:has-text('×'), span:has-text('×'), i:has-text('×')"
)
GLOBAL_MODAL_CLOSE_SELECTOR = (
    ".ant-modal-close:visible, .el-dialog__headerbtn:visible, .layui-layer-close:visible, "
    ".ui-dialog-titlebar-close:visible, [aria-label='Close']:visible, [aria-label='close']:visible, "
    "[aria-label='关闭']:visible, [title='关闭']:visible, [data-dismiss='modal']:visible, "
    "[data-bs-dismiss='modal']:visible, .modal:visible .close:visible, "
    ".modal-dialog:visible .close:visible, .modal-content:visible .close:visible, "
    "[role=dialog]:visible [class*='close' i]:visible, "
    "[class*='modal' i]:visible [class*='close' i]:visible, "
    "[class*='dialog' i]:visible [class*='close' i]:visible, "
    "[class*='popup' i]:visible [class*='close' i]:visible, "
    "[class*='pop' i]:visible [class*='close' i]:visible, "
    "button:has-text('关闭'):visible, a:has-text('关闭'):visible, [role=button]:has-text('关闭'):visible, "
    "button:has-text('我知道了'):visible, a:has-text('我知道了'):visible, "
    "button:has-text('知道了'):visible, a:has-text('知道了'):visible"
)


@dataclass
class RunResult:
    status: str
    steps: list[RunStep]


@dataclass
class ModalGuardStats:
    closed_count: int = 0
    last_error: str = ""


@dataclass
class RepairOutcome:
    step: RunStep
    applied: bool = False


_OPEN_BROWSER_SESSIONS: list[dict[str, Any]] = []
_RUNNER_LOOP: asyncio.AbstractEventLoop | None = None
_RUNNER_THREAD: threading.Thread | None = None
_RUNNER_LOCK = threading.Lock()


def run_workflow_sync(workflow: Workflow, headless: bool | None = None, on_step: Any | None = None) -> RunResult:
    loop = ensure_runner_loop()
    future = asyncio.run_coroutine_threadsafe(
        PlaywrightWorkflowExecutor(headless=headless, on_step=on_step).run(workflow),
        loop,
    )
    return future.result()


def ensure_runner_loop() -> asyncio.AbstractEventLoop:
    global _RUNNER_LOOP, _RUNNER_THREAD
    with _RUNNER_LOCK:
        if _RUNNER_LOOP and _RUNNER_THREAD and _RUNNER_THREAD.is_alive():
            return _RUNNER_LOOP
        loop = asyncio.new_event_loop()

        def run_loop() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()

        thread = threading.Thread(target=run_loop, name="ai-rpa-runner", daemon=True)
        thread.start()
        _RUNNER_LOOP = loop
        _RUNNER_THREAD = thread
        return loop


class PlaywrightWorkflowExecutor:
    def __init__(self, headless: bool | None = None, on_step: Any | None = None) -> None:
        if headless is None:
            headless = os.getenv("AI_RPA_HEADLESS", "false").lower() == "true"
        self.headless = headless
        self.modal_guard_enabled = os.getenv("AI_RPA_MODAL_GUARD", "true").lower() != "false"
        self.ai_repair_enabled = os.getenv("AI_RPA_AI_REPAIR", "true").lower() != "false"
        self.keep_browser_open = os.getenv("AI_RPA_KEEP_BROWSER_OPEN", "true").lower() != "false"
        self.on_step = on_step
        self.modal_guard_until = 0.0

    async def run(self, workflow: Workflow) -> RunResult:
        steps: list[RunStep] = []
        playwright = None
        context = None
        launched_new_context = False
        try:
            if self.keep_browser_open:
                session = reusable_browser_session()
                if session:
                    playwright = session["playwright"]
                    context = session["context"]
                    await self.record_step(steps, RunStep(
                        node_id="browser-session",
                        title="浏览器会话",
                        status="pass",
                        detail="reusing kept browser session",
                    ))
            if context is None:
                from playwright.async_api import async_playwright

                playwright = await async_playwright().start()
                context, profile_detail = await self.launch_browser_context(playwright)
                launched_new_context = True
                if profile_detail:
                    await self.record_step(steps, RunStep(
                        node_id="browser-profile",
                        title="浏览器 Profile",
                        status="pass",
                        detail=profile_detail,
                    ))
            page = context.pages[0] if context.pages else await context.new_page()
            self.active_page = page
            modal_guard_stats = ModalGuardStats()
            modal_guard_stop = asyncio.Event()
            modal_guard_task = (
                asyncio.create_task(self.run_modal_guard(page, modal_guard_stop, modal_guard_stats))
                if self.modal_guard_enabled
                else None
            )
            try:
                index = 0
                variables: dict[str, Any] = {}
                self.current_variables = variables
                loop_stack: list[dict[str, Any]] = []
                result_status = "pass"
                while index < len(workflow.nodes):
                    self.current_variables = variables
                    page = self.active_page
                    node = workflow.nodes[index]
                    if node.disabled and node.type not in {"flow.else", "flow.end_if", "flow.end_loop"}:
                        next_index = index + 1
                        if node.type == "flow.if":
                            next_index = find_end_if(workflow.nodes, index) + 1
                        elif node.type == "flow.loop":
                            next_index = find_end_loop(workflow.nodes, index) + 1
                        await self.record_step(steps, RunStep(
                            node_id=node.id,
                            title=node.title,
                            status="pass",
                            detail="skipped disabled node" if next_index == index + 1 else "skipped disabled control block",
                        ))
                        index = next_index
                        continue
                    if node.type == "flow.if":
                        condition = await self.evaluate_condition(page, node)
                        await self.record_step(steps, passed(node, f"condition {'passed' if condition else 'failed'}"))
                        if not condition:
                            index = find_else_or_end_if(workflow.nodes, index) + 1
                        else:
                            index += 1
                        continue
                    if node.type == "flow.else":
                        await self.record_step(steps, passed(node, "else skipped after true IF branch"))
                        index = find_end_if(workflow.nodes, index) + 1
                        continue
                    if node.type == "flow.end_if":
                        await self.record_step(steps, passed(node, "end if"))
                        index += 1
                        continue
                    if node.type == "flow.loop":
                        times = int(float(node.params.get("times", 1)))
                        start_index = int(float(node.params.get("startIndex", 1)))
                        if times <= 0:
                            await self.record_step(steps, passed(node, "loop skipped"))
                            index = find_end_loop(workflow.nodes, index) + 1
                            continue
                        if not loop_stack or loop_stack[-1]["start"] != index:
                            loop_stack.append({"id": node.id, "start": index, "remaining": times, "times": times, "startIndex": start_index})
                        current_loop = loop_stack[-1]
                        iteration = int(current_loop["times"]) - int(current_loop["remaining"]) + 1
                        item_index = int(current_loop.get("startIndex", 1)) + iteration - 1
                        self.set_loop_variables(variables, str(current_loop["id"]), item_index, int(current_loop["times"]))
                        await self.record_step(steps, passed(node, f"loop iteration {iteration}/{current_loop['times']} (item {item_index})"))
                        index += 1
                        continue
                    if node.type == "flow.end_loop":
                        if not loop_stack:
                            await self.record_step(steps, failed(node, "end_loop without matching loop"))
                            result_status = "fail"
                            break
                        current = loop_stack[-1]
                        current["remaining"] -= 1
                        if current["remaining"] > 0:
                            await self.record_step(steps, passed(node, f"loop continuing, remaining {current['remaining']}"))
                            index = current["start"]
                        else:
                            loop_stack.pop()
                            self.clear_loop_variables(variables, str(current["id"]))
                            await self.record_step(steps, passed(node, "loop finished"))
                            index += 1
                        continue
                    step = await self.execute_node(page, workflow, node, variables)
                    if step.status == "fail":
                        await self.record_step(steps, step)
                        repair = await self.try_repair_failed_node(self.active_page, workflow, index, node, step)
                        await self.record_step(steps, repair.step)
                        if repair.applied:
                            retry_step = await self.execute_node(self.active_page, workflow, node, variables)
                            await self.record_step(steps, retry_step)
                            if retry_step.status == "pass":
                                if bool(node.params.get("stopAfter", False)):
                                    result_status = "pass"
                                    break
                                index += 1
                                continue
                        skip_index = self.skip_to_next_product_if_possible(workflow.nodes, index)
                        if skip_index is not None:
                            cleanup_detail = await self.recover_list_page_after_skip(workflow)
                            await self.record_step(steps, RunStep(
                                node_id=f"{node.id}-skip-product",
                                title="跳过当前商品",
                                status="pass",
                                detail=self.skip_product_detail(
                                    node,
                                    retry_step.detail if repair.applied else step.detail,
                                    cleanup_detail,
                                ),
                            ))
                            variables = {}
                            self.current_variables = variables
                            index = skip_index
                            continue
                        result_status = "fail"
                        break
                    await self.record_step(steps, step)
                    if bool(node.params.get("stopAfter", False)):
                        result_status = "pass"
                        break
                    index += 1
                return RunResult(status=result_status, steps=steps)
            finally:
                modal_guard_stop.set()
                if modal_guard_task:
                    try:
                        await asyncio.wait_for(modal_guard_task, timeout=2)
                    except Exception:
                        modal_guard_task.cancel()
                if modal_guard_stats.closed_count:
                    await self.record_step(steps, RunStep(
                        node_id="modal-guard-subflow",
                        title="弹窗监控子流程",
                        status="pass",
                        detail=f"closed {modal_guard_stats.closed_count} modal(s) in parallel",
                    ))
                if modal_guard_stats.last_error:
                    await self.record_step(steps, RunStep(
                        node_id="modal-guard-subflow",
                        title="弹窗监控子流程",
                        status="fail",
                        detail=modal_guard_stats.last_error,
                    ))
                if self.keep_browser_open:
                    if launched_new_context:
                        _OPEN_BROWSER_SESSIONS.append({"playwright": playwright, "context": context})
                    await self.record_step(steps, RunStep(
                        node_id="browser-session",
                        title="浏览器会话",
                        status="pass",
                        detail="workflow stopped; browser kept open for inspection",
                    ))
                else:
                    await context.close()
                    if playwright is not None:
                        await playwright.stop()
        except Exception:
            if context is None or not self.keep_browser_open:
                try:
                    if context is not None:
                        await context.close()
                finally:
                    if playwright is not None:
                        await playwright.stop()
            raise

    async def run_without_browser_for_test(self, workflow: Workflow) -> RunResult:
        steps: list[RunStep] = []
        index = 0
        result_status = "pass"
        variables: dict[str, Any] = {}
        self.current_variables = variables
        loop_stack: list[dict[str, Any]] = []
        while index < len(workflow.nodes):
            self.current_variables = variables
            page = self.active_page
            node = workflow.nodes[index]
            if node.disabled and node.type not in {"flow.else", "flow.end_if", "flow.end_loop"}:
                next_index = index + 1
                if node.type == "flow.if":
                    next_index = find_end_if(workflow.nodes, index) + 1
                elif node.type == "flow.loop":
                    next_index = find_end_loop(workflow.nodes, index) + 1
                await self.record_step(steps, RunStep(
                    node_id=node.id,
                    title=node.title,
                    status="pass",
                    detail="skipped disabled node" if next_index == index + 1 else "skipped disabled control block",
                ))
                index = next_index
                continue
            if node.type == "flow.if":
                condition = await self.evaluate_condition(page, node)
                await self.record_step(steps, passed(node, f"condition {'passed' if condition else 'failed'}"))
                index = index + 1 if condition else find_else_or_end_if(workflow.nodes, index) + 1
                continue
            if node.type == "flow.else":
                await self.record_step(steps, passed(node, "else skipped after true IF branch"))
                index = find_end_if(workflow.nodes, index) + 1
                continue
            if node.type == "flow.end_if":
                await self.record_step(steps, passed(node, "end if"))
                index += 1
                continue
            if node.type == "flow.loop":
                times = int(float(node.params.get("times", 1)))
                start_index = int(float(node.params.get("startIndex", 1)))
                if times <= 0:
                    await self.record_step(steps, passed(node, "loop skipped"))
                    index = find_end_loop(workflow.nodes, index) + 1
                    continue
                if not loop_stack or loop_stack[-1]["start"] != index:
                    loop_stack.append({"id": node.id, "start": index, "remaining": times, "times": times, "startIndex": start_index})
                current_loop = loop_stack[-1]
                iteration = int(current_loop["times"]) - int(current_loop["remaining"]) + 1
                item_index = int(current_loop.get("startIndex", 1)) + iteration - 1
                self.set_loop_variables(variables, str(current_loop["id"]), item_index, int(current_loop["times"]))
                await self.record_step(steps, passed(node, f"loop iteration {iteration}/{current_loop['times']} (item {item_index})"))
                index += 1
                continue
            if node.type == "flow.end_loop":
                if not loop_stack:
                    await self.record_step(steps, failed(node, "end_loop without matching loop"))
                    result_status = "fail"
                    break
                current = loop_stack[-1]
                current["remaining"] -= 1
                if current["remaining"] > 0:
                    await self.record_step(steps, passed(node, f"loop continuing, remaining {current['remaining']}"))
                    index = current["start"]
                else:
                    loop_stack.pop()
                    self.clear_loop_variables(variables, str(current["id"]))
                    await self.record_step(steps, passed(node, "loop finished"))
                    index += 1
                continue
            step = await self.execute_node(page, workflow, node, variables)
            await self.record_step(steps, step)
            if step.status == "fail":
                skip_index = self.skip_to_next_product_if_possible(workflow.nodes, index)
                if skip_index is not None:
                    cleanup_detail = await self.recover_list_page_after_skip(workflow)
                    await self.record_step(steps, RunStep(
                        node_id=f"{node.id}-skip-product",
                        title="跳过当前商品",
                        status="pass",
                        detail=self.skip_product_detail(node, step.detail, cleanup_detail),
                    ))
                    variables = {}
                    self.current_variables = variables
                    index = skip_index
                    continue
                result_status = "fail"
                break
            if bool(node.params.get("stopAfter", False)):
                result_status = "pass"
                break
            index += 1
        return RunResult(status=result_status, steps=steps)

    def skip_to_next_product_if_possible(self, nodes: list[WorkflowNode], failed_index: int) -> int | None:
        loop_index = self.find_enclosing_loop_start(nodes, failed_index)
        if loop_index is None:
            return None
        loop_id = nodes[loop_index].id
        if loop_id != "loop-products":
            return None
        end_loop_index = find_end_loop(nodes, loop_index)
        return end_loop_index

    def find_enclosing_loop_start(self, nodes: list[WorkflowNode], node_index: int) -> int | None:
        stack: list[int] = []
        for index, node in enumerate(nodes[: node_index + 1]):
            if node.type == "flow.loop":
                stack.append(index)
                continue
            if node.type == "flow.end_loop" and stack:
                stack.pop()
        return stack[-1] if stack else None

    def set_loop_variables(self, variables: dict[str, Any], loop_id: str, iteration: int, times: int) -> None:
        zero_based_index = max(0, iteration - 1)
        set_variable_path(variables, "loop.id", loop_id)
        set_variable_path(variables, "loop.index", iteration)
        set_variable_path(variables, "loop.zeroBasedIndex", zero_based_index)
        set_variable_path(variables, "loop.times", times)
        set_variable_path(variables, f"{loop_id}.index", iteration)
        set_variable_path(variables, f"{loop_id}.zeroBasedIndex", zero_based_index)
        set_variable_path(variables, f"{loop_id}.times", times)

    def clear_loop_variables(self, variables: dict[str, Any], loop_id: str) -> None:
        loop_variables = variables.get("loop")
        if isinstance(loop_variables, dict):
            loop_variables.clear()
        scoped_variables = variables.get(loop_id)
        if isinstance(scoped_variables, dict):
            scoped_variables.clear()

    async def recover_list_page_after_skip(self, workflow: Workflow) -> str:
        page = getattr(self, "active_page", None)
        if page is None or self.page_matches_context(workflow, page, "page2"):
            return ""
        context = getattr(page, "context", None)
        pages = getattr(context, "pages", None)
        if not isinstance(pages, list):
            return ""
        list_page = self.find_page_by_context(workflow, [candidate for candidate in pages if candidate is not page], "page2")
        if list_page is None:
            return ""
        next_page = await self.close_current_tab_and_switch(page, workflow, "page2")
        self.active_page = next_page
        await self.wait_page_ready(next_page)
        return f"；已关闭当前详情页并返回列表页={getattr(next_page, 'url', '')}"

    def page_matches_context(self, workflow: Workflow, page: Any, context_id: str) -> bool:
        return self.find_page_by_context(workflow, [page], context_id) is not None

    def skip_product_detail(self, node: WorkflowNode, failed_detail: str, cleanup_detail: str = "") -> str:
        reason = shorten_detail(failed_detail or "未知原因", 280)
        return (
            f"当前商品执行失败，已跳过并继续下一个商品；"
            f"失败节点={node.id}；原因={reason}{cleanup_detail}"
        )

    async def launch_browser_context(self, playwright: Any) -> tuple[Any, str]:
        executable_path = os.getenv("AI_RPA_BROWSER_EXECUTABLE", "").strip() or None
        profile_dir = Path(os.getenv("AI_RPA_BROWSER_PROFILE", "browser-profile")).resolve()
        try:
            return await self.launch_persistent_context(playwright, profile_dir, executable_path), ""
        except Exception as error:
            if not self.keep_browser_open or not is_profile_busy_error(error):
                raise
            fallback_root = Path(os.getenv("AI_RPA_BROWSER_FALLBACK_PROFILE_ROOT", tempfile.gettempdir())).resolve()
            fallback_dir = fallback_root / f"ai-rpa-browser-profile-{os.getpid()}-{len(_OPEN_BROWSER_SESSIONS) + 1}"
            fallback_dir.mkdir(parents=True, exist_ok=True)
            context = await self.launch_persistent_context(playwright, fallback_dir, executable_path)
            return context, f"profile busy: {profile_dir}; using temporary profile: {fallback_dir}"

    async def launch_persistent_context(self, playwright: Any, profile_dir: Path, executable_path: str | None) -> Any:
        return await playwright.chromium.launch_persistent_context(
            str(profile_dir),
            headless=self.headless,
            executable_path=executable_path,
            viewport=None,
            args=["--start-maximized"],
        )

    async def record_step(self, steps: list[RunStep], step: RunStep) -> None:
        steps.append(step)
        if self.on_step:
            result = self.on_step(step)
            if asyncio.iscoroutine(result):
                await result

    async def hover_without_scroll(self, page: Any, locator: Any) -> None:
        target = await locator.evaluate(
            """element => {
              let hoverElement = element;
              let rect = hoverElement.getBoundingClientRect();
              if ((rect.width <= 1 || rect.height <= 1) && element.parentElement) {
                hoverElement = element.parentElement;
                rect = hoverElement.getBoundingClientRect();
              }
              const x = rect.left + Math.max(1, rect.width / 2);
              const y = rect.top + Math.max(1, rect.height / 2);
              const inViewport = rect.width > 0 && rect.height > 0
                && x >= 0 && y >= 0
                && x <= window.innerWidth && y <= window.innerHeight;
              return { x, y, inViewport };
            }""",
            timeout=1500,
        )
        if target.get("inViewport"):
            await page.mouse.move(float(target["x"]), float(target["y"]), steps=8)
            await page.wait_for_timeout(600)
            return
        await locator.evaluate(
            """element => {
              let hoverElement = element;
              let rect = hoverElement.getBoundingClientRect();
              if ((rect.width <= 1 || rect.height <= 1) && element.parentElement) {
                hoverElement = element.parentElement;
                rect = hoverElement.getBoundingClientRect();
              }
              const clientX = rect.left + Math.max(1, rect.width / 2);
              const clientY = rect.top + Math.max(1, rect.height / 2);
              const common = { bubbles: true, cancelable: true, composed: true, clientX, clientY };
              for (const type of ['pointerover', 'pointerenter']) {
                hoverElement.dispatchEvent(new PointerEvent(type, { ...common, pointerType: 'mouse', isPrimary: true }));
              }
              for (const type of ['mouseover', 'mouseenter', 'mousemove']) {
                hoverElement.dispatchEvent(new MouseEvent(type, { ...common, view: window }));
              }
            }""",
            timeout=1500,
        )
        await page.wait_for_timeout(600)

    async def click_without_scroll(self, page: Any, locator: Any) -> None:
        await locator.evaluate(
            """element => {
              element.click();
            }""",
            timeout=1500,
        )
        await page.wait_for_timeout(150)

    async def click_with_offset(self, page: Any, locator: Any, offset_x: float, offset_y: float, origin: str = "center") -> None:
        point = await locator.evaluate(
            """(element, payload) => {
              const rect = element.getBoundingClientRect();
              const origin = String(payload.origin || 'center');
              let x = rect.left;
              let y = rect.top;
              if (origin === 'center') {
                x += rect.width / 2;
                y += rect.height / 2;
              } else if (origin === 'right-center') {
                x += rect.width;
                y += rect.height / 2;
              }
              return {
                x: x + Number(payload.offsetX || 0),
                y: y + Number(payload.offsetY || 0),
                width: rect.width,
                height: rect.height
              };
            }""",
            {"offsetX": offset_x, "offsetY": offset_y, "origin": origin},
            timeout=1500,
        )
        await page.mouse.click(float(point["x"]), float(point["y"]))
        await page.wait_for_timeout(150)

    async def scroll_before_locate(self, page: Any, node: WorkflowNode, target_selector: str) -> None:
        container_selector = str(
            node.params.get("scrollBeforeLocateContainer")
            or ".ant-select-dropdown:visible:not(.ant-select-dropdown-hidden) .rc-virtual-list-holder"
        ).strip()
        if not container_selector:
            return
        steps = int(float(node.params.get("scrollBeforeLocateSteps", 8)))
        delta = int(float(node.params.get("scrollBeforeLocateDelta", 260)))
        container = page.locator(container_selector).first
        if callable(container):
            container = container()
        await container.wait_for(state="visible", timeout=int(float(node.params.get("timeoutMs", 10000))))
        for _ in range(max(1, steps)):
            try:
                if await page.locator(target_selector).first.is_visible(timeout=250):
                    return
            except Exception:
                pass
            await container.evaluate(
                """(element, deltaY) => {
                  element.scrollTop = Math.min(element.scrollHeight, element.scrollTop + deltaY);
                  element.dispatchEvent(new Event('scroll', { bubbles: true }));
                }""",
                delta,
                timeout=1500,
            )
            await page.wait_for_timeout(180)

    async def search_dropdown_before_locate(self, page: Any, node: WorkflowNode, target_selector: str) -> None:
        value = resolve_runtime_value(str(node.params.get("searchBeforeLocate") or ""), {})
        if not value.strip():
            return
        self.last_dropdown_options = []
        input_selector = str(
            node.params.get("searchBeforeLocateInput")
            or ".ant-select-open input.ant-select-selection-search-input, "
               ".ant-select-open .ant-select-selection-search input, "
               ".ant-select-dropdown:visible:not(.ant-select-dropdown-hidden) input"
        ).strip()
        option_selector = str(
            node.params.get("searchBeforeLocateOptions")
            or ".ant-select-dropdown:visible:not(.ant-select-dropdown-hidden) .ant-select-item-option, "
               "[role=option]:visible"
        ).strip()
        timeout_ms = int(float(node.params.get("timeoutMs", 10000)))
        search_input = page.locator(input_selector).first
        if callable(search_input):
            search_input = search_input()
        await search_input.wait_for(state="visible", timeout=timeout_ms)
        await search_input.fill(value)
        await page.wait_for_timeout(int(float(node.params.get("searchBeforeLocateWaitMs", 600))))
        try:
            if await page.locator(target_selector).first.is_visible(timeout=800):
                return
        except Exception:
            pass
        try:
            visible_options = await page.locator(option_selector).evaluate_all(
                """elements => elements
                  .filter(element => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none'
                      && rect.width > 0 && rect.height > 0;
                  })
                  .slice(0, 20)
                  .map(element => (element.innerText || element.textContent || '').trim())
                  .filter(Boolean)
                """,
                timeout=1500,
            )
        except Exception:
            visible_options = []
        if visible_options:
            self.last_dropdown_options = visible_options

    async def scroll_locator(self, page: Any, locator: Any, node: WorkflowNode) -> dict[str, float]:
        position = str(node.params.get("position") or node.params.get("direction") or "bottom").strip().lower()
        steps = int(float(node.params.get("scrollSteps", node.params.get("repeat", 8))))
        delta = int(float(node.params.get("scrollDelta", 320)))
        settle_ms = int(float(node.params.get("settleMs", 180)))
        last_state = {"before": 0.0, "after": 0.0, "max": 0.0}
        for _ in range(max(1, steps)):
            if position in {"bottom", "end"}:
                last_state = await locator.evaluate(
                    """element => {
                      const before = Number(element.scrollTop || 0);
                      element.scrollTop = Number(element.scrollHeight || 0);
                      element.dispatchEvent(new Event('scroll', { bubbles: true }));
                      return {
                        before,
                        after: Number(element.scrollTop || 0),
                        max: Math.max(0, Number(element.scrollHeight || 0) - Number(element.clientHeight || 0))
                      };
                    }""",
                    timeout=1500,
                )
            elif position in {"top", "start"}:
                last_state = await locator.evaluate(
                    """element => {
                      const before = Number(element.scrollTop || 0);
                      element.scrollTop = 0;
                      element.dispatchEvent(new Event('scroll', { bubbles: true }));
                      return {
                        before,
                        after: Number(element.scrollTop || 0),
                        max: Math.max(0, Number(element.scrollHeight || 0) - Number(element.clientHeight || 0))
                      };
                    }""",
                    timeout=1500,
                )
            elif position in {"down"}:
                last_state = await locator.evaluate(
                    """(element, deltaY) => {
                      const before = Number(element.scrollTop || 0);
                      element.scrollTop = Math.min(Number(element.scrollHeight || 0), before + Number(deltaY || 0));
                      element.dispatchEvent(new Event('scroll', { bubbles: true }));
                      return {
                        before,
                        after: Number(element.scrollTop || 0),
                        max: Math.max(0, Number(element.scrollHeight || 0) - Number(element.clientHeight || 0))
                      };
                    }""",
                    delta,
                    timeout=1500,
                )
            elif position in {"up"}:
                last_state = await locator.evaluate(
                    """(element, deltaY) => {
                      const before = Number(element.scrollTop || 0);
                      element.scrollTop = Math.max(0, before - Number(deltaY || 0));
                      element.dispatchEvent(new Event('scroll', { bubbles: true }));
                      return {
                        before,
                        after: Number(element.scrollTop || 0),
                        max: Math.max(0, Number(element.scrollHeight || 0) - Number(element.clientHeight || 0))
                      };
                    }""",
                    delta,
                    timeout=1500,
                )
            else:
                raise RuntimeError(f"unsupported scroll position: {position}")
            await page.wait_for_timeout(settle_ms)
        return {
            "before": float(last_state.get("before", 0.0)),
            "after": float(last_state.get("after", 0.0)),
            "max": float(last_state.get("max", 0.0)),
        }

    async def execute_node(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> RunStep:
        try:
            if node.disabled:
                return passed(node, "skipped disabled node")
            context_mismatch = await self.page_context_mismatch(page, workflow, node)
            if context_mismatch:
                return failed(node, context_mismatch)
            if node.type == "web.open":
                await page.goto(resolve_url(resolve_runtime_value(required_param(node, "url"), variables)), wait_until="domcontentloaded")
                await self.wait_page_ready(page)
                self.arm_modal_guard()
                return passed(node, f"opened {page.url}")
            if node.type == "web.scroll":
                locator = await self.locator_for(page, node)
                state = await self.scroll_locator(page, locator, node)
                return passed(
                    node,
                    f"scrolled {node.params.get('target', node.title)} to {node.params.get('position', 'bottom')}; "
                    f"top={state['after']:.0f}/{state['max']:.0f}"
                )
            if node.type == "web.input":
                raw_value = str(node.params.get("valueExpression") or node.params.get("value", ""))
                unresolved_refs = unresolved_runtime_references(raw_value, variables)
                if unresolved_refs and not bool(node.params.get("allowMissingValue", False)):
                    raise RuntimeError(f"输入值变量未生成或为空：{', '.join(unresolved_refs)}")
                if node.params.get("valueExpression"):
                    value = resolve_numeric_expression(raw_value, variables)
                else:
                    value = resolve_runtime_value(raw_value, variables)
                if extract_runtime_references(raw_value) and not value and not bool(node.params.get("allowEmpty", False)):
                    raise RuntimeError(f"输入值为空，已阻止写入：{raw_value}")
                if bool(node.params.get("rejectCjk", False)) and contains_cjk(value):
                    raise RuntimeError(f"输入值包含中文/CJK字符，已阻止写入：{node.params.get('target', node.title)}={value}")
                locator = await self.locator_for(page, node)
                if bool(node.params.get("richTextKeepImagesOnly", False)):
                    prefix_text = ""
                    prefix_type_value = str(node.params.get("richTextPrefixFromProductType") or "").strip()
                    if prefix_type_value:
                        product_type = resolve_runtime_value(prefix_type_value, variables)
                        prefix_text = pc_description_prefix_for_product_type(product_type)
                    await self.clean_rich_text_keep_images(locator, prefix_text=prefix_text)
                    prefix_detail = "; inserted size prompt" if prefix_text else ""
                    return passed(node, f"cleaned {node.params.get('target', node.title)}; kept images{prefix_detail}")
                await locator.fill(value)
                if bool(node.params.get("commitInput", False)):
                    await self.commit_input_value(locator, value)
                if bool(node.params.get("verifyInputValue", False)):
                    actual_value = await self.input_value(locator)
                    if actual_value != value:
                        raise RuntimeError(
                            f"输入后校验失败：{node.params.get('target', node.title)} 期望={value} 实际={actual_value}"
                        )
                    if bool(node.params.get("rejectCjk", False)) and contains_cjk(actual_value):
                        raise RuntimeError(f"输入后仍包含中文/CJK字符：{node.params.get('target', node.title)}={actual_value}")
                return passed(node, f"filled {node.params.get('target', node.title)}={value}")
            if node.type == "web.hover":
                locator = await self.locator_for(page, node)
                if bool(node.params.get("noScroll", False)):
                    await self.hover_without_scroll(page, locator)
                    return passed(node, f"hovered {node.params.get('target', node.title)} without scrolling")
                await locator.hover()
                return passed(node, f"hovered {node.params.get('target', node.title)}")
            if node.type == "web.click":
                locator = await self.locator_for(page, node)
                before_url = page.url
                popup = None
                self.suspend_modal_guard()
                pressed_before_click = False
                press_before_click = str(node.params.get("pressBeforeClick") or "").strip()
                if press_before_click:
                    await page.keyboard.press(press_before_click)
                    pressed_before_click = True
                if pressed_before_click and bool(node.params.get("skipClickAfterPress", False)):
                    pass
                elif "clickOffsetX" in node.params or "clickOffsetY" in node.params:
                    await self.click_with_offset(
                        page,
                        locator,
                        float(node.params.get("clickOffsetX", 0)),
                        float(node.params.get("clickOffsetY", 0)),
                        str(node.params.get("clickOffsetOrigin") or "center"),
                    )
                elif bool(node.params.get("noScroll", False)):
                    await self.click_without_scroll(page, locator)
                else:
                    clicked = False
                    try:
                        async with page.context.expect_page(timeout=int(float(node.params.get("popupTimeoutMs", 3500)))) as popup_info:
                            await locator.click()
                            clicked = True
                        popup = await popup_info.value
                    except Exception as error:
                        if "Timeout" not in str(error) or not clicked:
                            raise
                next_url = node.params.get("nextUrl")
                if popup:
                    await popup.bring_to_front()
                    self.active_page = popup
                    page = popup
                    await self.wait_page_ready(page)
                press_after_click = str(node.params.get("pressAfterClick") or "").strip()
                if press_after_click:
                    press_if_visible = str(node.params.get("pressAfterClickIfVisible") or "").strip()
                    should_press = True
                    if press_if_visible:
                        try:
                            should_press = await page.locator(press_if_visible).first.is_visible(timeout=800)
                        except Exception:
                            should_press = False
                    if should_press:
                        await page.keyboard.press(press_after_click)
                if next_url:
                    await page.wait_for_timeout(1200)
                    await page.goto(resolve_url(resolve_runtime_value(str(next_url), variables)), wait_until="domcontentloaded")
                    await self.wait_page_ready(page)
                    self.arm_modal_guard()
                elif page.url != before_url:
                    await self.wait_page_ready(page)
                if bool(node.params.get("skipAfterClickWaits", False)):
                    tab_detail = f"; switched to new tab {page.url}" if popup else ""
                    return passed(node, f"clicked {node.params.get('target', node.title)}{tab_detail}")
                wait_hidden = resolve_runtime_value(str(node.params.get("waitAfterClickHidden") or "").strip(), variables)
                if wait_hidden:
                    await page.locator(wait_hidden).first.wait_for(
                        state="hidden",
                        timeout=int(float(node.params.get("waitAfterClickTimeoutMs", node.params.get("timeoutMs", 10000)))),
                    )
                wait_visible = resolve_runtime_value(str(node.params.get("waitAfterClickVisible") or "").strip(), variables)
                if wait_visible:
                    await page.locator(wait_visible).first.wait_for(
                        state="visible",
                        timeout=int(float(node.params.get("waitAfterClickTimeoutMs", node.params.get("timeoutMs", 10000)))),
                    )
                tab_detail = f"; switched to new tab {page.url}" if popup else ""
                return passed(node, f"clicked {node.params.get('target', node.title)}{tab_detail}")
            if node.type == "web.wait_for":
                await self.wait_for(page, node, variables)
                return passed(node, f"waited for {node.params}")
            if node.type == "web.select":
                locator = await self.locator_for(page, node)
                value = resolve_runtime_value(str(node.params.get("value", "")), variables)
                await locator.select_option(value)
                return passed(node, f"selected {value} for {node.params.get('target', node.title)}")
            if node.type == "web.extract":
                locator = await self.locator_for(page, node)
                text = await locator.text_content()
                save_as = str(node.params.get("saveAs") or "").strip()
                if save_as:
                    set_variable_path(variables, save_as, text or "")
                extracted_groups = extract_regex_groups(
                    text or "",
                    node.params.get("regex"),
                    node.params.get("groupSaveAs"),
                    bool(node.params.get("trim", True)),
                )
                for path, value in extracted_groups.items():
                    set_variable_path(variables, path, value)
                group_detail = f"; groups={extracted_groups}" if extracted_groups else ""
                return passed(node, f"extracted {node.params.get('target', node.title)}: {shorten_detail(text or '')}{group_detail}")
            if node.type == "web.close_modals":
                closed = await self.close_page_modals(page)
                return passed(node, f"closed {closed} modal(s)")
            if node.type == "web.close_tab":
                switch_context_id = str(
                    node.params.get("switchToPageContext")
                    or node.params.get("switchToContext")
                    or ""
                ).strip()
                next_page = await self.close_current_tab_and_switch(page, workflow, switch_context_id)
                self.active_page = next_page
                await self.wait_page_ready(next_page)
                return passed(
                    node,
                    f"closed current tab; switched to {next_page.url}"
                    + (f" for {switch_context_id}" if switch_context_id else ""),
                )
            if node.type == "flow.wait":
                await page.wait_for_timeout(int(float(node.params.get("seconds", 1)) * 1000))
                return passed(node, f"waited {node.params.get('seconds', 1)}s")
            if node.type == "ai.ask":
                detail = await self.execute_ai_ask(page, workflow, node, variables)
                return passed(node, detail)
            return failed(node, f"node type not implemented yet: {node.type}")
        except Exception as error:
            return failed(node, str(error))

    async def page_context_mismatch(self, page: Any, workflow: Workflow, node: WorkflowNode) -> str:
        if node.type in {"web.open", "web.wait_for", "flow.wait"}:
            return ""
        context = page_context_payload(workflow, node)
        context_id = context.get("id")
        page_object = context.get("object")
        if not context_id or not isinstance(page_object, dict) or not page_object:
            return ""
        current_url = page.url
        url_includes = [str(item) for item in page_object.get("urlIncludes", []) if str(item)]
        if url_includes and not any(item in current_url for item in url_includes):
            return (
                f"页面上下文不匹配：节点要求 {context_id}（{page_object.get('name', '')}），"
                f"但当前 URL 是 {current_url}，未命中 urlIncludes={url_includes}"
            )
        url_excludes = [str(item) for item in page_object.get("urlExcludes", []) if str(item)]
        matched_excludes = [item for item in url_excludes if item in current_url]
        if matched_excludes:
            return (
                f"页面上下文不匹配：节点要求 {context_id}（{page_object.get('name', '')}），"
                f"但当前 URL 是 {current_url}，命中了禁止 URL 片段 {matched_excludes}"
            )
        html_hints = [str(item) for item in page_object.get("htmlHints", []) if str(item)]
        if html_hints:
            try:
                text = await page.locator("body").inner_text(timeout=1500)
            except Exception:
                text = compact_html_snapshot(await page.content(), 12000)
            if not any(hint in text for hint in html_hints):
                return (
                    f"页面上下文不匹配：节点要求 {context_id}（{page_object.get('name', '')}），"
                    f"但当前页面未出现详情页关键文本 htmlHints={html_hints}"
                )
        return ""

    async def execute_ai_ask(self, page: Any, workflow: Workflow, node: WorkflowNode, variables: dict[str, Any]) -> str:
        from .mimo_client import MimoClient

        prompt = resolve_runtime_value(str(node.params.get("prompt") or ""), variables)
        if not prompt.strip():
            raise RuntimeError("ai.ask requires params.prompt")
        screenshot_path = None
        html_snapshot = ""
        if bool(node.params.get("screenshot", True)):
            screenshot_dir = Path(os.getenv("AI_RPA_SCREENSHOT_DIR", "screenshots")).resolve()
            screenshot_dir.mkdir(exist_ok=True)
            safe_node_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", node.id)
            screenshot_path = screenshot_dir / f"{safe_node_id}.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)
        if bool(node.params.get("html", True)):
            html_max_chars = int(float(node.params.get("htmlMaxChars", os.getenv("AI_RPA_HTML_MAX_CHARS", "50000"))))
            html_snapshot = compact_html_snapshot(await page.content(), html_max_chars)
        page_title = ""
        try:
            page_title = await page.title()
        except Exception:
            pass
        expected_format = node.params.get("expectedFormat")
        if not isinstance(expected_format, dict):
            expected_format = {
                "summary": "你在页面上看到了什么",
                "confidence": 0.0,
                "nextAction": "建议下一步动作",
                "selectors": ["可用于自动化的候选 selector"],
                "riskNotes": []
            }
        result = MimoClient().complete_json(
            (
                "你是一个 RPA 页面判断 Agent。你会同时收到页面截图和压缩后的 HTML/DOM 片段，"
                "必须结合视觉布局、HTML 结构、表单属性、按钮文本和 selector 线索判断。"
                "只返回 JSON，用于后续 workflow 节点参考。必须严格包含用户给出的 expectedFormat 字段。"
                "验证码、captcha、图形码、安全校验只能识别并提示人工处理，"
                "不能建议点击、聚焦、填写、读取、OCR 或绕过。"
            ),
            json.dumps({
                "url": page.url,
                "title": page_title,
                "pageContext": page_context_payload(workflow, node),
                "task": prompt,
                "evidence": {
                    "screenshotIncluded": bool(screenshot_path),
                    "htmlIncluded": bool(html_snapshot),
                    "htmlSnapshot": html_snapshot,
                },
                "expectedFormat": expected_format,
            }, ensure_ascii=False),
            screenshot_path=str(screenshot_path) if screenshot_path else None,
            retries=max(1, int(float(node.params.get("retries", os.getenv("MIMO_REQUEST_RETRIES", "3"))))),
            timeout=max(5, int(float(node.params.get("timeoutSeconds", os.getenv("MIMO_REQUEST_TIMEOUT", "60"))))),
        )
        result = normalize_ai_result(result, node)
        save_as = str(node.params.get("saveAs") or node.id).strip()
        set_variable_path(variables, save_as, result)
        return (
            f"saved {save_as}; html={len(html_snapshot)} chars; "
            f"mimo={shorten_detail(json.dumps(result, ensure_ascii=False))}"
        )

    async def try_repair_failed_node(
        self,
        page: Any,
        workflow: Workflow,
        node_index: int,
        node: WorkflowNode,
        failed_step: RunStep,
    ) -> RepairOutcome:
        non_repairable_markers = (
            "输入值变量未生成或为空",
            "输入值为空",
            "运行变量缺失",
            "数值表达式计算失败",
            "extracted text did not match regex",
            "regex group not found",
        )
        if any(marker in failed_step.detail for marker in non_repairable_markers):
            return RepairOutcome(RunStep(
                node_id=f"{node.id}-ai-repair",
                title="AI 修复 workflow",
                status="fail",
                detail="AI repair skipped for missing runtime data or numeric expression failure",
            ))
        if not self.ai_repair_enabled or not (node.type.startswith("web.") or node.type == "ai.ask"):
            return RepairOutcome(RunStep(
                node_id=f"{node.id}-ai-repair",
                title="AI 修复 workflow",
                status="fail",
                detail="AI repair disabled or unsupported node type",
            ))
        from .mimo_client import MimoClient

        try:
            screenshot_dir = Path(os.getenv("AI_RPA_SCREENSHOT_DIR", "screenshots")).resolve()
            screenshot_dir.mkdir(exist_ok=True)
            safe_node_id = re.sub(r"[^a-zA-Z0-9_.-]+", "-", node.id)
            screenshot_path = screenshot_dir / f"repair-{safe_node_id}.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)
            html_max_chars = int(float(os.getenv("AI_RPA_REPAIR_HTML_MAX_CHARS", os.getenv("AI_RPA_HTML_MAX_CHARS", "60000"))))
            html_snapshot = compact_html_snapshot(await page.content(), html_max_chars)
            page_title = ""
            try:
                page_title = await page.title()
            except Exception:
                pass
            decision = MimoClient().complete_json(
                (
                    "你是 AI RPA workflow 修复 Agent。当前某个 web 节点执行失败。"
                    "你会收到失败节点、错误、相邻节点、当前 URL、截图和 HTML。"
                    "请判断是否能仅修改失败节点的 params 来修复，例如 selector、target、timeoutMs。"
                    "只有当你非常确定时才 canAutoFix=true；否则给出 question 询问用户。"
                    "selector 必须是 Playwright 可执行 CSS/text selector，不要使用 jQuery :contains。"
                    "验证码、captcha、图形码、安全校验不能点击、聚焦、填写、读取、OCR 或绕过。"
                    "只返回 JSON。"
                ),
                json.dumps({
                    "url": page.url,
                    "title": page_title,
                    "pageContext": page_context_payload(workflow, node),
                    "failedNodeIndex": node_index,
                    "failedNode": node_to_dict(node),
                    "error": failed_step.detail,
                    "nearbyNodes": [
                        {"index": index, **node_to_dict(item)}
                        for index, item in nearby_nodes(workflow.nodes, node_index)
                    ],
                    "htmlSnapshot": html_snapshot,
                    "expectedFormat": {
                        "confidence": 0.0,
                        "canAutoFix": False,
                        "reason": "为什么可以或不可以自动修复",
                        "question": "不确定时问用户的问题",
                        "nodePatch": {
                            "params": {
                                "selector": "修复后的 selector",
                                "target": "可选",
                                "timeoutMs": 15000
                            }
                        }
                    },
                }, ensure_ascii=False),
                screenshot_path=str(screenshot_path),
            )
            confidence = float(decision.get("confidence") or 0)
            patch = decision.get("nodePatch") if isinstance(decision.get("nodePatch"), dict) else {}
            if bool(decision.get("canAutoFix")) and confidence >= 0.75 and patch:
                changed = apply_node_patch(node, patch)
                if changed:
                    return RepairOutcome(
                        RunStep(
                            node_id=f"{node.id}-ai-repair",
                            title="AI 修复 workflow",
                            status="pass",
                            detail=(
                                f"已根据截图和 DOM 修复当前节点并重试；confidence={confidence}; "
                                f"changed={', '.join(changed)}; reason={shorten_detail(str(decision.get('reason') or ''), 220)}"
                            ),
                        ),
                        applied=True,
                    )
            question = str(decision.get("question") or "").strip()
            reason = str(decision.get("reason") or "").strip()
            return RepairOutcome(RunStep(
                node_id=f"{node.id}-ai-repair",
                title="AI 修复 workflow",
                status="fail",
                detail=(
                    f"AI 不确定，已停止等待用户确认；confidence={confidence}; "
                    f"reason={shorten_detail(reason, 220)}; question={shorten_detail(question, 260)}"
                ),
            ))
        except Exception as error:
            return RepairOutcome(RunStep(
                node_id=f"{node.id}-ai-repair",
                title="AI 修复 workflow",
                status="fail",
                detail=f"AI 修复失败：{error}",
            ))

    async def wait_page_ready(self, page: Any) -> None:
        for state, timeout in [("domcontentloaded", 5000), ("load", 5000), ("networkidle", 2500)]:
            try:
                await page.wait_for_load_state(state, timeout=timeout)
            except Exception:
                pass
        await page.wait_for_timeout(300)

    def arm_modal_guard(self) -> None:
        seconds = float(os.getenv("AI_RPA_MODAL_GUARD_SECONDS", "4"))
        self.modal_guard_until = asyncio.get_running_loop().time() + max(0.0, seconds)

    def suspend_modal_guard(self) -> None:
        self.modal_guard_until = 0.0

    def modal_guard_active(self) -> bool:
        return asyncio.get_running_loop().time() < self.modal_guard_until

    async def run_modal_guard(self, page: Any, stop_event: asyncio.Event, stats: ModalGuardStats) -> None:
        while not stop_event.is_set():
            try:
                if self.modal_guard_active():
                    active_page = getattr(self, "active_page", page)
                    stats.closed_count += await self.close_page_modals(active_page)
            except Exception as error:
                stats.last_error = str(error)
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=0.45)
            except asyncio.TimeoutError:
                pass

    async def close_page_modals(self, page: Any) -> int:
        closed = 0
        for _ in range(4):
            modal_index = await self.find_auto_close_modal_index(page)
            if modal_index is None:
                break
            clicked = await self.click_modal_close_button(page, modal_index)
            if not clicked:
                break
            closed += 1
            await page.wait_for_timeout(300)
        return closed

    async def find_auto_close_modal_index(self, page: Any) -> int | None:
        for index, modal in enumerate(await self.describe_visible_modals(page)):
            if should_auto_close_modal(
                str(modal.get("text") or ""),
                [str(item) for item in modal.get("buttonTexts", []) if str(item).strip()],
                bool(modal.get("hasFormFields", False)),
                bool(modal.get("hasStructuredContent", False)),
                bool(modal.get("hasDismissControl", False)),
            ):
                return index
        return None

    async def describe_visible_modals(self, page: Any) -> list[dict[str, Any]]:
        custom_describer = getattr(page, "describe_visible_modals", None)
        if callable(custom_describer):
            described = custom_describer()
            if asyncio.iscoroutine(described):
                described = await described
            if isinstance(described, list):
                return [item for item in described if isinstance(item, dict)]
            return []
        try:
            return await page.locator(MODAL_CONTAINER_SELECTOR).evaluate_all(
                """(elements, closeSelector) => elements
                  .filter(element => {
                    const style = window.getComputedStyle(element);
                    const rect = element.getBoundingClientRect();
                    return style.visibility !== 'hidden' && style.display !== 'none'
                      && rect.width > 0 && rect.height > 0;
                  })
                  .map(element => {
                    const text = (element.innerText || element.textContent || '').trim();
                    const buttonTexts = Array.from(element.querySelectorAll('button, a, [role="button"]'))
                      .map(item => (item.innerText || item.textContent || '').trim())
                      .filter(Boolean)
                      .slice(0, 12);
                    const hasFormFields = !!element.querySelector('input, textarea, select, form');
                    const hasStructuredContent = !!element.querySelector('table, .ant-form, .el-form, [contenteditable="true"], iframe, .cke, .ant-select');
                    const hasDismissControl = !!element.querySelector(closeSelector)
                      || buttonTexts.some(text => text && ['关闭', '取消', '我知道了', '知道了', 'close', 'cancel', 'dismiss', 'skip', 'later'].includes(text.toLowerCase().replace(/\\s+/g, '')));
                    return { text, buttonTexts, hasFormFields, hasStructuredContent, hasDismissControl };
                  })
                """,
                MODAL_CLOSE_ICON_DOM_SELECTOR,
            )
        except Exception:
            return []

    async def close_current_tab_and_switch(self, page: Any, workflow: Workflow, switch_context_id: str = "") -> Any:
        context = page.context
        current_url = page.url
        await page.close()
        remaining_pages = [candidate for candidate in context.pages if candidate is not page]
        if not remaining_pages:
            raise RuntimeError(f"关闭标签页后没有可切换页面：{current_url}")
        next_page = self.find_page_by_context(workflow, remaining_pages, switch_context_id)
        if next_page is None:
            next_page = remaining_pages[-1]
        await next_page.bring_to_front()
        return next_page

    def find_page_by_context(self, workflow: Workflow, pages: list[Any], context_id: str) -> Any | None:
        if not context_id:
            return None
        page_object = workflow.pageObjects.get(context_id, {})
        if not isinstance(page_object, dict):
            return None
        url_includes = [str(item) for item in page_object.get("urlIncludes", []) if str(item)]
        url_excludes = [str(item) for item in page_object.get("urlExcludes", []) if str(item)]
        for candidate in pages:
            candidate_url = getattr(candidate, "url", "") or ""
            if url_includes and not any(item in candidate_url for item in url_includes):
                continue
            if url_excludes and any(item in candidate_url for item in url_excludes):
                continue
            return candidate
        return None

    async def has_visible_modal_container(self, page: Any) -> bool:
        container = page.locator(MODAL_CONTAINER_SELECTOR).first
        if callable(container):
            container = container()
        try:
            return await container.count() > 0 and await container.is_visible(timeout=800)
        except Exception:
            return False

    async def click_modal_close_button(self, page: Any, modal_index: int = 0) -> bool:
        custom_clicker = getattr(page, "click_modal_close_by_index", None)
        if callable(custom_clicker):
            clicked = custom_clicker(modal_index)
            if asyncio.iscoroutine(clicked):
                clicked = await clicked
            return bool(clicked)
        try:
            container = page.locator(MODAL_CONTAINER_SELECTOR).nth(modal_index)
            close_button = container.locator(MODAL_CLOSE_SELECTOR).first
            if callable(close_button):
                close_button = close_button()
            if await self.click_if_visible(close_button):
                return True
        except Exception:
            pass

        global_close_button = page.locator(GLOBAL_MODAL_CLOSE_SELECTOR).first
        if callable(global_close_button):
            global_close_button = global_close_button()
        return await self.click_if_visible(global_close_button)

    async def click_if_visible(self, locator: Any) -> bool:
        try:
            if await locator.count() > 0 and await locator.is_visible(timeout=800):
                await locator.click(timeout=1200)
                return True
        except Exception:
            return False
        return False

    async def evaluate_condition(self, page: Any, node: WorkflowNode) -> bool:
        variables = getattr(self, "current_variables", {})
        selector = resolve_runtime_value(str(node.params.get("selector") or "").strip(), variables)
        text = resolve_runtime_value(str(node.params.get("text") or "").strip(), variables)
        url_includes = resolve_runtime_value(str(node.params.get("urlIncludes") or "").strip(), variables)
        negate = bool(node.params.get("negate", False))
        if selector and is_captcha_target(selector):
            raise RuntimeError("captcha-like selector cannot be used in IF")
        if text and is_captcha_target(text):
            raise RuntimeError("captcha-like text cannot be used in IF")
        if selector:
            if bool(node.params.get("scrollBeforeLocate", False)):
                try:
                    await self.scroll_before_locate(page, node, selector)
                except Exception:
                    pass
            result = await page.locator(selector).first.is_visible(timeout=1000)
        elif text:
            result = await page.get_by_text(text).first.is_visible(timeout=1000)
        elif url_includes:
            result = url_includes in page.url
        else:
            result = False
        return not result if negate else result

    async def locator_for(self, page: Any, node: WorkflowNode) -> Any:
        variables = getattr(self, "current_variables", {})
        selector = resolve_runtime_value(str(node.params.get("selector") or "").strip(), variables)
        target = resolve_runtime_value(str(node.params.get("target") or node.title), variables)
        timeout_ms = int(float(node.params.get("timeoutMs", 10000)))
        state = str(node.params.get("state") or "attached")
        if is_captcha_target(target):
            raise RuntimeError("captcha targets require manual handling and cannot be clicked or filled")
        if selector:
            if node.params.get("searchBeforeLocate"):
                try:
                    if not await page.locator(selector).first.is_visible(timeout=800):
                        await self.search_dropdown_before_locate(page, node, selector)
                except Exception:
                    await self.search_dropdown_before_locate(page, node, selector)
            if bool(node.params.get("scrollBeforeLocate", False)):
                await self.scroll_before_locate(page, node, selector)
            locator = page.locator(selector).first
            if callable(locator):
                locator = locator()
            try:
                await locator.wait_for(state=state, timeout=timeout_ms)
            except Exception as error:
                options = getattr(self, "last_dropdown_options", [])
                option_detail = f"；当前下拉选项={options}" if options else ""
                raise RuntimeError(
                    f"未找到可操作元素：{target}；selector={shorten_detail(selector, 320)}{option_detail}"
                ) from error
            await self.ensure_not_captcha(locator, target)
            return locator
        locator = page.get_by_text(target).first
        if callable(locator):
            locator = locator()
        try:
            await locator.wait_for(state=state, timeout=timeout_ms)
        except Exception as error:
            raise RuntimeError(f"未找到文本元素：{target}") from error
        await self.ensure_not_captcha(locator, target)
        return locator

    async def ensure_not_captcha(self, locator: Any, target: str = "元素") -> None:
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
            ].filter(Boolean).join(' ')""",
            timeout=1500,
        )
        if CAPTCHA_PATTERN.search(signal or ""):
            raise RuntimeError(f"refusing to interact with captcha-like element: {target}")

    async def commit_input_value(self, locator: Any, value: str) -> None:
        await locator.evaluate(
            """(element, value) => {
              const prototype = element instanceof HTMLTextAreaElement
                ? HTMLTextAreaElement.prototype
                : HTMLInputElement.prototype;
              const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
              if (setter) {
                setter.call(element, value);
              } else {
                element.value = value;
              }
              element.dispatchEvent(new Event('input', { bubbles: true }));
              element.dispatchEvent(new Event('change', { bubbles: true }));
              element.blur();
            }""",
            value,
            timeout=1500,
        )

    async def clean_rich_text_keep_images(self, locator: Any, prefix_text: str = "") -> None:
        await locator.evaluate(
            """async (element, prefixText) => {
              const textarea = element instanceof HTMLTextAreaElement
                ? element
                : element.querySelector('textarea[id^="ckeditor"]') || element;
              const editorId = textarea.id || textarea.getAttribute('name') || '';
              const instance = window.CKEDITOR && editorId ? window.CKEDITOR.instances[editorId] : null;
              const currentHtml = instance && typeof instance.getData === 'function'
                ? instance.getData()
                : String(textarea.value || textarea.getAttribute('value') || textarea.innerHTML || '');
              const holder = document.createElement('div');
              holder.innerHTML = currentHtml;
              const images = Array.from(holder.querySelectorAll('img')).map(img => img.outerHTML);
              const escapeHtml = value => String(value || '')
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
              const prefixHtml = String(prefixText || '').trim()
                ? `<pre style="white-space: pre-wrap; font-family: inherit;">${escapeHtml(prefixText)}</pre>`
                : '';
              const nextHtml = prefixHtml + images.map(markup => `<p>${markup}</p>`).join('');
              if (instance && typeof instance.setData === 'function') {
                await new Promise(resolve => {
                  let resolved = false;
                  const finish = () => {
                    if (resolved) return;
                    resolved = true;
                    if (typeof instance.updateElement === 'function') {
                      instance.updateElement();
                    }
                    resolve();
                  };
                  try {
                    instance.setData(nextHtml, finish);
                    setTimeout(finish, 800);
                  } catch (error) {
                    finish();
                  }
                });
              } else if (textarea.isContentEditable) {
                textarea.innerHTML = nextHtml;
              }
              if (textarea instanceof HTMLTextAreaElement || textarea instanceof HTMLInputElement) {
                const prototype = textarea instanceof HTMLTextAreaElement
                  ? HTMLTextAreaElement.prototype
                  : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(prototype, 'value')?.set;
                if (setter) {
                  setter.call(textarea, nextHtml);
                } else {
                  textarea.value = nextHtml;
                }
                textarea.setAttribute('value', nextHtml);
              }
              textarea.dispatchEvent(new Event('input', { bubbles: true }));
              textarea.dispatchEvent(new Event('change', { bubbles: true }));
              textarea.blur();
            }""",
            prefix_text,
            timeout=2500,
        )

    async def input_value(self, locator: Any) -> str:
        value = await locator.evaluate(
            "element => String(element.value ?? '')",
            timeout=1500,
        )
        return str(value)

    async def wait_for(self, page: Any, node: WorkflowNode, variables: dict[str, Any] | None = None) -> None:
        url_includes = node.params.get("urlIncludes")
        text = node.params.get("text")
        selector = node.params.get("selector")
        input_values = node.params.get("inputValues")
        state = str(node.params.get("state") or "visible")
        timeout_ms = int(float(node.params.get("timeoutMs", 30000)))
        if url_includes:
            resolved_url_includes = resolve_runtime_value(str(url_includes), variables or {})
            await page.wait_for_url(lambda url: str(url).find(resolved_url_includes) >= 0, timeout=timeout_ms)
        if selector:
            await page.locator(resolve_runtime_value(str(selector), variables or {})).first.wait_for(state=state, timeout=timeout_ms)
        if text:
            await page.get_by_text(resolve_runtime_value(str(text), variables or {})).first.wait_for(state=state, timeout=timeout_ms)
        if input_values:
            await self.assert_input_values(page, node, input_values, variables or {})

    async def assert_input_values(
        self,
        page: Any,
        node: WorkflowNode,
        input_values: Any,
        variables: dict[str, Any],
    ) -> None:
        if not isinstance(input_values, list):
            raise RuntimeError("inputValues must be a list")
        timeout_ms = int(float(node.params.get("timeoutMs", 30000)))
        missing: list[str] = []
        for item in input_values:
            if not isinstance(item, dict):
                raise RuntimeError("inputValues item must be an object")
            target = str(item.get("target") or item.get("selector") or "输入框")
            selector = resolve_runtime_value(str(item.get("selector") or "").strip(), variables)
            if not selector:
                raise RuntimeError(f"inputValues missing selector: {target}")
            raw_value = str(item.get("valueExpression") or item.get("value") or "")
            expected = resolve_numeric_expression(raw_value, variables) if item.get("valueExpression") else resolve_runtime_value(raw_value, variables)
            locator = page.locator(selector).first
            if callable(locator):
                locator = locator()
            await locator.wait_for(state="visible", timeout=timeout_ms)
            actual = await self.input_value(locator)
            if actual != expected:
                missing.append(f"{target}: 期望={expected} 实际={actual or '<空>'}")
        if missing:
            raise RuntimeError("输入框检查失败：" + "；".join(missing))


def is_captcha_target(target: str) -> bool:
    return bool(CAPTCHA_PATTERN.search(target))


def find_else_or_end_if(nodes: list[WorkflowNode], start_index: int) -> int:
    depth = 0
    for index in range(start_index + 1, len(nodes)):
        node_type = nodes[index].type
        if node_type == "flow.if":
            depth += 1
        elif node_type == "flow.end_if":
            if depth == 0:
                return index
            depth -= 1
        elif node_type == "flow.else" and depth == 0:
            return index
    raise RuntimeError(f"flow.if at index {start_index} has no matching end_if")


def find_end_if(nodes: list[WorkflowNode], start_index: int) -> int:
    depth = 0
    for index in range(start_index + 1, len(nodes)):
        node_type = nodes[index].type
        if node_type == "flow.if":
            depth += 1
        elif node_type == "flow.end_if":
            if depth == 0:
                return index
            depth -= 1
    raise RuntimeError(f"flow.else at index {start_index} has no matching end_if")


def find_end_loop(nodes: list[WorkflowNode], start_index: int) -> int:
    depth = 0
    for index in range(start_index + 1, len(nodes)):
        node_type = nodes[index].type
        if node_type == "flow.loop":
            depth += 1
        elif node_type == "flow.end_loop":
            if depth == 0:
                return index
            depth -= 1
    raise RuntimeError(f"flow.loop at index {start_index} has no matching end_loop")


def reusable_browser_session() -> dict[str, Any] | None:
    while _OPEN_BROWSER_SESSIONS:
        session = _OPEN_BROWSER_SESSIONS[-1]
        context = session.get("context")
        try:
            pages = context.pages
            if pages is not None:
                return session
        except Exception:
            _OPEN_BROWSER_SESSIONS.pop()
    return None


def is_profile_busy_error(error: Exception) -> bool:
    message = str(error).lower()
    return (
        "existing browser session" in message
        or "现有的浏览器会话" in message
        or "target page, context or browser has been closed" in message
        or "process did exit" in message
        or "profile" in message and "lock" in message
    )


def resolve_env_value(value: str) -> str:
    if value.startswith("${") and value.endswith("}"):
        return os.getenv(value[2:-1], "")
    return value


def resolve_runtime_value(value: str, variables: dict[str, Any]) -> str:
    pattern = re.compile(r"\$\{([^}]+)\}")

    def replace(match: re.Match[str]) -> str:
        key = match.group(1).strip()
        function_value = resolve_runtime_function(key, variables)
        if function_value is not None:
            return function_value
        variable_value = get_variable_path(variables, key)
        if variable_value is not None:
            if isinstance(variable_value, (dict, list)):
                return json.dumps(variable_value, ensure_ascii=False)
            return str(variable_value)
        return os.getenv(key, "")

    if pattern.fullmatch(value):
        return replace(pattern.fullmatch(value))  # type: ignore[arg-type]
    return pattern.sub(replace, value)


def resolve_runtime_function(key: str, variables: dict[str, Any]) -> str | None:
    match = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)\(([^()]*)\)", key)
    if not match:
        return None
    function_name = match.group(1)
    argument = match.group(2).strip()
    if function_name not in {"freightTemplateByWeight", "shippingTemplateByWeight"}:
        return None
    weight_value = get_variable_path(variables, argument)
    if weight_value is None:
        weight_value = os.getenv(argument, argument)
    return freight_template_label_for_weight(weight_value)


def freight_template_label_for_weight(weight_value: Any) -> str:
    try:
        weight = Decimal(str(weight_value).strip())
    except InvalidOperation as error:
        raise RuntimeError(f"运费模板重量无效：{weight_value}") from error
    if weight <= 0:
        raise RuntimeError(f"运费模板重量必须大于 0：{weight_value}")
    if weight <= 50:
        return "0-50g"
    bucket = ((weight - Decimal("51")) // Decimal("100"))
    lower = bucket * Decimal("100") + Decimal("51")
    upper = lower + Decimal("99")
    return f"{format_decimal(lower)}-{format_decimal(upper)}g"


PC_DESCRIPTION_PROMPT = """Prompt:
1.Due to the lighting and display reasons, little color difference is normal, please forgive

2.Due to different measurement methods, there will be 1-3 cm error is a normal phenomenon

3.Our size is asia size normally smaller than US size

4.Pls take a minute to check size chart, if you are not sure about size details

5.1cm = 0.39 inches 1inch=2.54cm

6.Unit: cm"""

PC_DESCRIPTION_WARM_PROMPT = PC_DESCRIPTION_PROMPT.replace("Prompt:", "Warm Prompt:", 1)

PC_DESCRIPTION_PREFIX_BY_TYPE = {
    "连衣裙均码": """Size Chart
One Size       Length:cm      Bust:cm      Waist:cm      Sleeve:cm      Shoulder:cm
""" + PC_DESCRIPTION_PROMPT,
    "连衣裙多尺码": """Size Chart

S       Length:cm     Bust:cm     Waist:cm  Sleeve:cm       Shoulder:cm
M     Length:cm      Bust:cm    Waist:cm    Sleeve:cm       Shoulder:cm
L       Length:cm      Bust:cm    Waist:cm      Sleeve:cm      Shoulder:cm
XL    Length:cm      Bust:cm      Waist:cm     Sleeve:cm       Shoulder:cm
XXL  Length:cm      Bust:cm     Waist:cm      Sleeve:cm      Shoulder:cm
""" + PC_DESCRIPTION_PROMPT,
    "上衣均码": """Size Chart
One Size      Length:cm      Bust:cm   
""" + PC_DESCRIPTION_PROMPT,
    "半身裙均码": """Size Chart

One Size       Length:cm      Waist:cm

""" + PC_DESCRIPTION_WARM_PROMPT,
    "套装均码": """TOP
 Length:cm     Bust:cm     Sleeve:cm      Shoulder:cm
Bottom
 Length:cm     Waist:cm    Hip:cm

""" + PC_DESCRIPTION_PROMPT,
    "套装多尺码": """TOP
S       Length:cm     Bust:cm     Sleeve:cm       Shoulder:cm
M     Length:cm      Bust:cm    Sleeve:cm        Shoulder:cm
L       Length:cm      Bust:cm    Sleeve:cm        Shoulder:cm
XL    Length:cm      Bust:cm      Sleeve:cm      Shoulder:cm
XXL  Length:cm      Bust:cm     Sleeve:cm        Shoulder:cm
Bottom
S       Length:cm     Waist:cm    Hip:cm
M     Length:cm      Waist:cm     Hip:cm
L       Length:cm      Waist:cm     Hip:cm
XL     Length:cm      Waist:cm     Hip:cm
XXL   Length:cm      Waist:cm      Hip:cm
""" + PC_DESCRIPTION_PROMPT,
}


def pc_description_prefix_for_product_type(product_type: str) -> str:
    normalized = "".join(str(product_type or "").split())
    if not normalized:
        return ""
    for key, value in PC_DESCRIPTION_PREFIX_BY_TYPE.items():
        if key in normalized:
            return value
    return ""


def resolve_numeric_expression(expression: str, variables: dict[str, Any]) -> str:
    resolved = resolve_runtime_value(expression, variables)
    try:
        value = evaluate_decimal_expression(resolved)
    except (InvalidOperation, ValueError, ZeroDivisionError) as error:
        raise RuntimeError(f"数值表达式计算失败：{expression}") from error
    return format_decimal(value)


def evaluate_decimal_expression(expression: str) -> Decimal:
    tree = ast.parse(expression, mode="eval")
    return evaluate_decimal_ast(tree.body)


def evaluate_decimal_ast(node: ast.AST) -> Decimal:
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return Decimal(str(node.value))
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
        operand = evaluate_decimal_ast(node.operand)
        return operand if isinstance(node.op, ast.UAdd) else -operand
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)):
        left = evaluate_decimal_ast(node.left)
        right = evaluate_decimal_ast(node.right)
        if isinstance(node.op, ast.Add):
            return left + right
        if isinstance(node.op, ast.Sub):
            return left - right
        if isinstance(node.op, ast.Mult):
            return left * right
        return left / right
    raise ValueError(f"unsupported numeric expression: {ast.dump(node)}")


def format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return str(normalized.quantize(Decimal("1")))
    return format(normalized, "f").rstrip("0").rstrip(".")


def extract_runtime_references(value: str) -> list[str]:
    references: list[str] = []
    for raw_reference in re.findall(r"\$\{([^}]+)\}", value):
        reference = raw_reference.strip()
        if not reference:
            continue
        function_match = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*\(([^()]*)\)", reference)
        if function_match:
            argument = function_match.group(1).strip()
            if argument and not re.fullmatch(r"-?\d+(?:\.\d+)?", argument):
                references.append(argument)
            continue
        references.append(reference)
    return references


def unresolved_runtime_references(value: str, variables: dict[str, Any]) -> list[str]:
    missing: list[str] = []
    for reference in extract_runtime_references(value):
        variable_value = get_variable_path(variables, reference)
        if variable_value is None and os.getenv(reference, "") == "":
            missing.append(reference)
    return missing


def normalize_ai_result(result: dict[str, Any], node: WorkflowNode) -> dict[str, Any]:
    expected_format = node.params.get("expectedFormat")
    requires_title = isinstance(expected_format, dict) and "title" in expected_format
    if not requires_title:
        return result
    if not str(result.get("title") or "").strip():
        for key in ["translatedTitle", "englishTitle", "result", "answer", "text", "content", "value"]:
            value = result.get(key)
            if isinstance(value, str) and value.strip():
                result["title"] = value
                break
    if str(result.get("title") or "").strip():
        result["title"] = normalize_ai_title(str(result["title"]))
        if bool(node.params.get("rejectCjkTitle", False)) and contains_cjk(str(result["title"])):
            raise RuntimeError(f"AI 标题仍包含中文/CJK字符：{result['title']}")
    return result


def contains_cjk(value: str) -> bool:
    return bool(CJK_PATTERN.search(value))


def normalize_ai_title(value: str, max_length: int = 99) -> str:
    title = re.sub(r"\s+", " ", value).strip()
    title = title.strip("\"'“”‘’")
    if len(title) <= max_length:
        return title
    truncated = title[:max_length].rstrip()
    if " " in truncated:
        truncated = truncated.rsplit(" ", 1)[0].rstrip()
    return truncated or title[:max_length].rstrip()


def get_variable_path(variables: dict[str, Any], path: str) -> Any:
    current: Any = variables
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def set_variable_path(variables: dict[str, Any], path: str, value: Any) -> None:
    parts = [part for part in path.split(".") if part]
    if not parts:
        return
    current = variables
    for part in parts[:-1]:
        child = current.get(part)
        if not isinstance(child, dict):
            child = {}
            current[part] = child
        current = child
    current[parts[-1]] = value


def extract_regex_groups(
    value: str,
    regex: Any,
    group_save_as: Any,
    trim: bool = True,
) -> dict[str, str]:
    if not regex and not group_save_as:
        return {}
    if not isinstance(regex, str) or not regex.strip():
        raise RuntimeError("web.extract groupSaveAs requires params.regex")
    if not isinstance(group_save_as, dict) or not group_save_as:
        raise RuntimeError("web.extract regex requires params.groupSaveAs")
    match = re.search(regex, value, flags=re.DOTALL)
    if not match:
        raise RuntimeError(f"extracted text did not match regex: {shorten_detail(regex, 220)}")
    extracted: dict[str, str] = {}
    for group_name, save_path in group_save_as.items():
        if not isinstance(save_path, str) or not save_path.strip():
            continue
        try:
            group_value = match.group(str(group_name))
        except IndexError as error:
            raise RuntimeError(f"regex group not found: {group_name}") from error
        extracted[save_path.strip()] = group_value.strip() if trim else group_value
    return extracted


def resolve_url(value: str) -> str:
    if re.match(r"^[a-z][a-z0-9+.-]*://", value, flags=re.IGNORECASE):
        return value
    path = Path(value)
    if not path.is_absolute():
        cwd_path = Path.cwd() / path
        resource_path = bundled_root() / path
        path = cwd_path if cwd_path.exists() else resource_path
    return path.resolve().as_uri()


def required_param(node: WorkflowNode, key: str) -> str:
    value = node.params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"node {node.id} missing required param: {key}")
    return value


def passed(node: WorkflowNode, detail: str) -> RunStep:
    return RunStep(node_id=node.id, title=node.title, status="pass", detail=detail)


def failed(node: WorkflowNode, detail: str) -> RunStep:
    return RunStep(node_id=node.id, title=node.title, status="fail", detail=detail)


def modal_detail(count: int) -> str:
    return f"; closed {count} modal(s)" if count else ""


def should_auto_close_modal(
    text: str,
    button_texts: list[str],
    has_form_fields: bool,
    has_structured_content: bool,
    has_dismiss_control: bool,
) -> bool:
    if not has_dismiss_control:
        return False
    if has_form_fields or has_structured_content:
        return False
    normalized_buttons = [normalize_modal_button_text(item) for item in button_texts if normalize_modal_button_text(item)]
    if any(button not in DISMISSIVE_MODAL_BUTTON_TEXTS for button in normalized_buttons):
        return False
    if BUSINESS_MODAL_TEXT_PATTERN.search(text or ""):
        return False
    return True


DISMISSIVE_MODAL_BUTTON_TEXTS = {
    "关闭",
    "取消",
    "我知道了",
    "知道了",
    "close",
    "cancel",
    "dismiss",
    "later",
}

BUSINESS_MODAL_TEXT_PATTERN = re.compile(
    r"编辑分类|去编辑产品|无线端内容|一键生成|选择产品信息模块|新版编辑器|物流属性|"
    r"自定义属性|确定删除|保存并下一步|欧盟责任人|品牌制造商|服务模板|运费模板|"
    r"编辑产品|商品详情|产品标题|确定.*吗",
    re.IGNORECASE,
)


def normalize_modal_button_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def node_to_dict(node: WorkflowNode) -> dict[str, Any]:
    return {
        "id": node.id,
        "type": node.type,
        "title": node.title,
        "params": dict(node.params),
    }


def nearby_nodes(nodes: list[WorkflowNode], index: int, radius: int = 2) -> list[tuple[int, WorkflowNode]]:
    start = max(0, index - radius)
    end = min(len(nodes), index + radius + 1)
    return list(enumerate(nodes[start:end], start=start))


def apply_node_patch(node: WorkflowNode, patch: dict[str, Any]) -> list[str]:
    raw_params = patch.get("params") if isinstance(patch.get("params"), dict) else patch
    if not isinstance(raw_params, dict):
        return []
    allowed_keys = {
        "selector",
        "target",
        "text",
        "urlIncludes",
        "value",
        "timeoutMs",
        "nextUrl",
    }
    changed: list[str] = []
    for key, value in raw_params.items():
        if key not in allowed_keys:
            continue
        if isinstance(value, str) and is_captcha_target(value):
            raise RuntimeError(f"AI repair refused captcha-like value for {key}")
        if node.params.get(key) != value:
            node.params[key] = value
            changed.append(f"params.{key}")
    return changed


def page_context_payload(workflow: Workflow, node: WorkflowNode) -> dict[str, Any]:
    context_id = str(
        node.params.get("pageContext")
        or node.params.get("pageObject")
        or node.params.get("page")
        or ""
    ).strip()
    if not context_id:
        return {"id": "", "object": {}, "note": "当前节点没有绑定页面对象"}
    page_object = workflow.pageObjects.get(context_id, {})
    return {
        "id": context_id,
        "object": page_object if isinstance(page_object, dict) else {},
        "note": f"当前节点必须按 {context_id} 的页面语境理解，不要套用其他页面对象。",
    }


def compact_html_snapshot(value: str, limit: int = 50000) -> str:
    compact = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
    compact = re.sub(r"<(script|style|noscript|svg|canvas)\b[^>]*>.*?</\1>", " ", compact, flags=re.DOTALL | re.IGNORECASE)
    compact = re.sub(r"\s(on[a-z]+)=(\"[^\"]*\"|'[^']*')", " ", compact, flags=re.IGNORECASE)
    compact = re.sub(r"\s(src|href)=(\"data:[^\"]*\"|'data:[^']*')", " ", compact, flags=re.IGNORECASE)
    compact = re.sub(r">\s+<", "><", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    if limit <= 0:
        return ""
    if len(compact) <= limit:
        return compact
    return compact[:limit] + "\n<!-- html truncated -->"


def shorten_detail(value: str, limit: int = 500) -> str:
    compact = " ".join(str(value).split())
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"
