from __future__ import annotations

import argparse
import json
import mimetypes
import os
from dataclasses import asdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .executor import run_workflow_sync
from .mimo_client import MimoClient
from .paths import bundled_root, env_file_candidates, static_dir, workflow_dir
from .workflow_loader import SUPPORTED_NODE_TYPES, WorkflowValidationError, load_workflow, parse_workflow


APP_ROOT = bundled_root()
WORKFLOW_DIR = workflow_dir()
STATIC_DIR = static_dir()

AGENT_SYSTEM_PROMPT = """你是一个专注 RPA 低代码 workflow 的规划 Agent。
你只负责把用户的自然语言请求转成一个安全、可审计的 workflow 修改动作。

支持的节点类型：
- web.open: params.url
- web.scroll: params.selector, 可选 params.position=top|bottom
- web.hover: params.target, params.selector
- web.click: params.target, params.selector
- web.input: params.target, params.selector, params.value
- web.wait_for: params.text 或 params.selector 或 params.urlIncludes
- web.select: params.target, params.selector, params.value
- web.extract: params.target, params.selector
- ai.ask: params.prompt
- flow.wait: params.seconds
- flow.if: params.selector 或 params.text 或 params.urlIncludes，可选 params.negate
- flow.else: params 可为空
- flow.end_if: params 可为空
- flow.loop: params.times
- flow.end_loop: params 可为空

只能返回 JSON，不要输出 Markdown。
返回格式：
{
  "reply": "给用户看的简短中文说明",
  "action": "insert_node | update_node | delete_node | replace_workflow | noop",
  "nodeIndex": 0,
  "insertAfterIndex": 0,
  "node": {"id": "node-id", "type": "web.click", "title": "点击元素", "params": {}},
  "workflow": {}
}

规则：
- 优先保留现有 workflow，只做用户要求的最小修改。
- 不要生成宏节点；必须生成具体一步一步可执行的节点。
- 如果节点只适用于某个页面对象，请在 params.pageContext 中写入对应 pageObjects id。
- 不允许点击、输入、读取验证码或 captcha 类元素。
- selector 不确定时也要给出可编辑的合理候选，并在 reply 中说明需要人工确认。
- selector 必须使用 Playwright/CSS 可执行形式，不要使用 jQuery 的 :contains()；文本匹配请用 :has-text("文本")。
- action 为 update_node/delete_node 时，nodeIndex 必须指向要操作的节点。
- action 为 insert_node 时，node 必须完整且 type 必须是支持的节点类型。
"""


def load_env_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    for env_file in env_file_candidates():
        if env_file.is_file():
            load_dotenv(env_file)
            return


def list_workflow_names() -> list[str]:
    return sorted(path.name for path in WORKFLOW_DIR.glob("*.json") if path.is_file())


def workflow_path(name: str) -> Path:
    if "/" in name or "\\" in name or name.startswith("."):
        raise ValueError("invalid workflow name")
    path = (WORKFLOW_DIR / name).resolve()
    if path.parent != WORKFLOW_DIR.resolve() or path.suffix != ".json":
        raise ValueError("invalid workflow path")
    return path


class AiRpaRequestHandler(BaseHTTPRequestHandler):
    server_version = "AiRpaStarter/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/health":
            self.send_json({"status": "ok"})
            return
        if parsed.path == "/":
            self.send_static_file(STATIC_DIR / "app.html")
            return
        if parsed.path.startswith("/static/"):
            self.handle_static_get(parsed.path)
            return
        if parsed.path == "/api/workflows":
            self.send_json({"workflows": list_workflow_names()})
            return
        if parsed.path == "/api/workflow":
            name = single_query_value(parsed.query, "name")
            self.handle_workflow_get(name)
            return
        if parsed.path == "/api/settings":
            mimo_configured = bool(os.getenv("MIMO_API_KEY", "").strip())
            mimo_healthy = False
            mimo_error = "MIMO_API_KEY is not configured"
            if mimo_configured:
                mimo_healthy, mimo_error = MimoClient().health_check()
            self.send_json({
                "mimoConfigured": mimo_configured,
                "mimoHealthy": mimo_healthy,
                "mimoError": "" if mimo_healthy else mimo_error,
                "headless": os.getenv("AI_RPA_HEADLESS", "false"),
                "browserExecutable": os.getenv("AI_RPA_BROWSER_EXECUTABLE", ""),
                "browserProfile": os.getenv("AI_RPA_BROWSER_PROFILE", "browser-profile"),
            })
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/workflow":
            name = single_query_value(parsed.query, "name")
            self.handle_workflow_save(name)
            return
        if parsed.path == "/api/validate":
            self.handle_validate()
            return
        if parsed.path == "/api/run":
            self.handle_run()
            return
        if parsed.path == "/api/run_stream":
            self.handle_run_stream()
            return
        if parsed.path == "/api/agent":
            self.handle_agent()
            return
        self.send_error(HTTPStatus.NOT_FOUND, "not found")

    def handle_workflow_get(self, name: str) -> None:
        try:
            path = workflow_path(name)
            self.send_json({"name": name, "content": path.read_text(encoding="utf-8")})
        except Exception as error:
            self.send_json({"error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def handle_workflow_save(self, name: str) -> None:
        try:
            raw = self.read_json()
            content = str(raw.get("content", ""))
            parsed = json.loads(content)
            parse_workflow(parsed)
            path = workflow_path(name)
            path.write_text(json.dumps(parsed, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            self.send_json({"status": "pass", "message": f"saved {name}"})
        except Exception as error:
            self.send_json({"status": "fail", "error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def handle_validate(self) -> None:
        try:
            raw = self.read_json()
            content = str(raw.get("content", ""))
            workflow = parse_workflow(json.loads(content))
            self.send_json({
                "status": "pass",
                "message": f"{workflow.name} / {len(workflow.nodes)} nodes",
            })
        except Exception as error:
            self.send_json({"status": "fail", "error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def handle_run(self) -> None:
        try:
            raw = self.read_json()
            name = str(raw.get("name", ""))
            headless = bool(raw.get("headless", False))
            content = str(raw.get("content", "")).strip()
            workflow = parse_workflow(json.loads(content)) if content else load_workflow(workflow_path(name))
            result = run_workflow_sync(workflow, headless=headless)
            payload = asdict(result)
            payload["workflow"] = asdict(workflow)
            self.send_json(payload, status=HTTPStatus.OK if result.status == "pass" else HTTPStatus.BAD_REQUEST)
        except Exception as error:
            self.send_json({"status": "fail", "error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def handle_run_stream(self) -> None:
        try:
            raw = self.read_json()
            name = str(raw.get("name", ""))
            headless = bool(raw.get("headless", False))
            content = str(raw.get("content", "")).strip()
            workflow = parse_workflow(json.loads(content)) if content else load_workflow(workflow_path(name))
        except Exception as error:
            self.send_json({"status": "fail", "error": str(error)}, status=HTTPStatus.BAD_REQUEST)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/event-stream; charset=utf-8")
        self.send_header("cache-control", "no-cache")
        self.send_header("connection", "close")
        self.end_headers()

        def send_sse(event: str, payload: dict[str, object]) -> None:
            data = json.dumps(payload, ensure_ascii=False)
            self.wfile.write(f"event: {event}\n".encode("utf-8"))
            self.wfile.write(f"data: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        try:
            disabled_nodes = [
                f"{index + 1}.{node.id}"
                for index, node in enumerate(workflow.nodes)
                if node.disabled
            ]
            send_sse("start", {
                "name": workflow.name,
                "nodeCount": len(workflow.nodes),
                "disabledNodeCount": len(disabled_nodes),
                "disabledNodes": disabled_nodes,
                "status": "running",
            })
            result = run_workflow_sync(
                workflow,
                headless=headless,
                on_step=lambda step: send_sse("step", asdict(step)),
            )
            payload = asdict(result)
            payload["workflow"] = asdict(workflow)
            send_sse("result", payload)
        except Exception as error:
            try:
                send_sse("error", {"status": "fail", "error": str(error)})
            except Exception:
                pass
        finally:
            self.close_connection = True

    def handle_agent(self) -> None:
        try:
            raw = self.read_json()
            message = str(raw.get("message", "")).strip()
            workflow = raw.get("workflow")
            selected_index = int(raw.get("selectedIndex", 0))
            elements = raw.get("elements") if isinstance(raw.get("elements"), list) else []
            image_data_url = str(raw.get("imageDataUrl") or "").strip()
            image_name = str(raw.get("imageName") or "").strip()
            if not message:
                raise ValueError("message is required")
            if not isinstance(workflow, dict):
                raise ValueError("workflow must be an object")
            if image_data_url:
                validate_image_data_url(image_data_url)
            parse_workflow(workflow)

            source = "mimo"
            model_error = ""
            try:
                decision = MimoClient().complete_json(
                    AGENT_SYSTEM_PROMPT,
                    build_agent_user_prompt(message, workflow, selected_index, elements, image_name, bool(image_data_url)),
                    attached_image_data_url=image_data_url or None,
                )
            except Exception as error:
                source = "fallback"
                model_error = str(error)
                decision = fallback_agent_decision(message, workflow, selected_index)

            result = apply_agent_decision(decision, workflow, selected_index)
            result["source"] = source
            if model_error:
                result["modelError"] = model_error
            self.send_json(result)
        except Exception as error:
            self.send_json({"status": "fail", "error": str(error)}, status=HTTPStatus.BAD_REQUEST)

    def handle_static_get(self, request_path: str) -> None:
        relative = request_path.removeprefix("/static/")
        if "/" in relative or "\\" in relative or relative.startswith("."):
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        self.send_static_file(STATIC_DIR / relative)

    def send_static_file(self, path: Path) -> None:
        resolved = path.resolve()
        if not resolved.is_file() or STATIC_DIR.resolve() not in resolved.parents:
            self.send_error(HTTPStatus.NOT_FOUND, "not found")
            return
        data = resolved.read_bytes()
        content_type = static_content_type(resolved)
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", content_type)
        self.send_header("content-length", str(len(data)))
        self.send_header("cache-control", "no-cache")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("content-length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        return json.loads(body or "{}")

    def send_html(self, html: str) -> None:
        data = html.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_json(self, payload: dict[str, object], status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json; charset=utf-8")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        return


def single_query_value(query: str, key: str) -> str:
    values = parse_qs(query).get(key, [])
    return values[0] if values else ""


def static_content_type(path: Path) -> str:
    if path.suffix == ".html":
        return "text/html; charset=utf-8"
    if path.suffix == ".css":
        return "text/css; charset=utf-8"
    if path.suffix == ".js":
        return "application/javascript; charset=utf-8"
    return mimetypes.guess_type(str(path))[0] or "application/octet-stream"


def validate_image_data_url(value: str) -> None:
    if len(value) > 8_000_000:
        raise ValueError("image attachment is too large")
    if not value.startswith(("data:image/png;base64,", "data:image/jpeg;base64,", "data:image/webp;base64,")):
        raise ValueError("image attachment must be png, jpeg, or webp data url")


def build_agent_user_prompt(
    message: str,
    workflow: dict[str, Any],
    selected_index: int,
    elements: list[Any],
    image_name: str = "",
    image_included: bool = False,
) -> str:
    compact_nodes = [
        {
            "index": index,
            "id": node.get("id"),
            "type": node.get("type"),
            "title": node.get("title"),
            "disabled": bool(node.get("disabled", False)),
            "params": node.get("params", {}),
        }
        for index, node in enumerate(workflow.get("nodes", []))
        if isinstance(node, dict)
    ]
    payload = {
        "userMessage": message,
        "selectedIndex": selected_index,
        "workflow": {
            "id": workflow.get("id"),
            "name": workflow.get("name"),
            "version": workflow.get("version"),
            "pageObjects": workflow.get("pageObjects", {}),
            "nodes": compact_nodes,
        },
        "elements": elements[:40],
        "imageAttachment": {
            "included": image_included,
            "name": image_name,
            "purpose": "用户提供的参考图/截图。请结合这张图识别网页中的目标区域、元素位置、文字标签和应生成或修改的 workflow 节点。",
        },
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def apply_agent_decision(
    decision: dict[str, Any],
    workflow: dict[str, Any],
    selected_index: int,
) -> dict[str, Any]:
    if not isinstance(decision, dict):
        raise ValueError("agent decision must be an object")
    updated = json.loads(json.dumps(workflow, ensure_ascii=False))
    nodes = updated.get("nodes")
    if not isinstance(nodes, list):
        raise ValueError("workflow.nodes must be a list")
    action = str(decision.get("action") or "noop")
    reply = str(decision.get("reply") or "已完成判断。")

    if action == "noop":
        pass
    elif action == "insert_node":
        node = sanitize_agent_node(decision.get("node"), updated)
        insert_after = clamp_index(int(decision.get("insertAfterIndex", selected_index)), -1, len(nodes) - 1)
        nodes.insert(insert_after + 1, node)
        selected_index = insert_after + 1
    elif action == "update_node":
        node_index = clamp_index(int(decision.get("nodeIndex", selected_index)), 0, len(nodes) - 1)
        patch = decision.get("node")
        if not isinstance(patch, dict):
            raise ValueError("update_node requires node patch")
        merged = dict(nodes[node_index])
        if "id" in patch:
            merged["id"] = str(patch["id"]).strip()
        if "type" in patch:
            merged["type"] = str(patch["type"]).strip()
        if "title" in patch:
            merged["title"] = str(patch["title"]).strip()
        if "params" in patch:
            if not isinstance(patch["params"], dict):
                raise ValueError("node.params must be an object")
            merged["params"] = patch["params"]
        if "indent" in patch:
            merged["indent"] = max(0, int(patch["indent"]))
        nodes[node_index] = sanitize_agent_node(merged, updated, current_index=node_index)
        selected_index = node_index
    elif action == "delete_node":
        if len(nodes) <= 1:
            raise ValueError("cannot delete the last node")
        node_index = clamp_index(int(decision.get("nodeIndex", selected_index)), 0, len(nodes) - 1)
        nodes.pop(node_index)
        selected_index = max(0, min(node_index, len(nodes) - 1))
    elif action == "replace_workflow":
        replacement = decision.get("workflow")
        if not isinstance(replacement, dict):
            raise ValueError("replace_workflow requires workflow")
        updated = replacement
        selected_index = min(selected_index, len(updated.get("nodes", [])) - 1)
    else:
        raise ValueError(f"unsupported agent action: {action}")

    parse_workflow(updated)
    return {
        "status": "pass",
        "reply": reply,
        "action": action,
        "workflow": updated,
        "selectedIndex": max(0, selected_index),
    }


def sanitize_agent_node(
    raw_node: Any,
    workflow: dict[str, Any],
    current_index: int | None = None,
) -> dict[str, Any]:
    if not isinstance(raw_node, dict):
        raise ValueError("node must be an object")
    node_type = str(raw_node.get("type") or "").strip()
    if node_type not in SUPPORTED_NODE_TYPES:
        raise ValueError(f"unsupported node type: {node_type}")
    node_id = str(raw_node.get("id") or node_type.replace(".", "-")).strip()
    if not node_id:
        node_id = node_type.replace(".", "-")
    node = {
        "id": unique_node_id(node_id, workflow, current_index),
        "type": node_type,
        "title": str(raw_node.get("title") or node_id).strip(),
        "params": sanitize_agent_params(raw_node.get("params") if isinstance(raw_node.get("params"), dict) else {}),
    }
    if "indent" in raw_node:
        node["indent"] = max(0, int(raw_node.get("indent") or 0))
    if "disabled" in raw_node:
        node["disabled"] = bool(raw_node.get("disabled"))
    return node


def sanitize_agent_params(params: dict[str, Any]) -> dict[str, Any]:
    sanitized = dict(params)
    selector = sanitized.get("selector")
    if isinstance(selector, str):
        sanitized["selector"] = normalize_selector(selector)
    return sanitized


def normalize_selector(selector: str) -> str:
    return re_sub_contains_selector(selector)


def re_sub_contains_selector(selector: str) -> str:
    import re

    return re.sub(
        r":contains\((['\"])(.*?)\1\)",
        lambda match: f':has-text("{match.group(2)}")',
        selector,
    )


def unique_node_id(base_id: str, workflow: dict[str, Any], current_index: int | None = None) -> str:
    existing = {
        str(node.get("id"))
        for index, node in enumerate(workflow.get("nodes", []))
        if isinstance(node, dict) and index != current_index
    }
    candidate = base_id
    suffix = 2
    while candidate in existing:
        candidate = f"{base_id}-{suffix}"
        suffix += 1
    return candidate


def fallback_agent_decision(message: str, workflow: dict[str, Any], selected_index: int) -> dict[str, Any]:
    if "删除" in message:
        return {
            "action": "delete_node",
            "nodeIndex": selected_index,
            "reply": "Mimo 暂时不可用，已用本地规则删除当前节点。",
        }
    template = fallback_node_template(message)
    if template:
        return {
            "action": "insert_node",
            "insertAfterIndex": selected_index,
            "node": template,
            "reply": f"Mimo 暂时不可用，已用本地规则添加节点：{template['title']}。",
        }
    return {
        "action": "noop",
        "reply": "Mimo 暂时不可用，本地规则没有足够信息修改 workflow。",
    }


def fallback_node_template(message: str) -> dict[str, Any] | None:
    if any(word in message for word in ["点击", "按钮", "单击"]):
        return {
            "id": "web-click",
            "type": "web.click",
            "title": "点击元素(web)",
            "params": {"target": "按钮", "selector": "button:has-text(\"确定\")"},
        }
    if any(word in message for word in ["悬停", "鼠标移入", "hover"]):
        return {
            "id": "web-hover",
            "type": "web.hover",
            "title": "悬停元素(web)",
            "params": {"target": "元素", "selector": "text=目标元素"},
        }
    if any(word in message for word in ["输入", "填写", "填入"]):
        return {
            "id": "web-input",
            "type": "web.input",
            "title": "填写输入框(web)",
            "params": {"target": "输入框", "selector": "input[name=value]", "value": ""},
        }
    if any(word in message for word in ["打开", "访问", "网址"]):
        return {
            "id": "web-open",
            "type": "web.open",
            "title": "打开网页",
            "params": {"url": "https://example.com"},
        }
    if any(word in message for word in ["下拉", "选择"]):
        return {
            "id": "web-select",
            "type": "web.select",
            "title": "下拉选择(web)",
            "params": {"target": "下拉框", "selector": "select", "value": ""},
        }
    if any(word in message for word in ["等待", "暂停"]):
        return {
            "id": "flow-wait",
            "type": "flow.wait",
            "title": "等待",
            "params": {"seconds": 1},
        }
    if any(word in message for word in ["IF", "if", "判断", "如果", "条件"]):
        return {
            "id": "flow-if",
            "type": "flow.if",
            "title": "IF 判断",
            "params": {"selector": "body", "negate": False},
        }
    if any(word in message for word in ["循环", "重复", "loop"]):
        return {
            "id": "flow-loop",
            "type": "flow.loop",
            "title": "循环",
            "params": {"times": 2},
        }
    if any(word in message for word in ["AI", "ai", "大模型", "判断", "生成"]):
        return {
            "id": "ai-ask",
            "type": "ai.ask",
            "title": "AI 生成/判断",
            "params": {"prompt": "请基于当前页面给出下一步动作"},
        }
    return None


def clamp_index(value: int, low: int, high: int) -> int:
    if high < low:
        return low
    return max(low, min(value, high))


def main() -> None:
    load_env_if_available()
    parser = argparse.ArgumentParser(description="Run AI RPA web app")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    run_app(args.host, args.port)


def run_app(host: str = "127.0.0.1", port: int = 8765) -> None:
    load_env_if_available()
    server = ThreadingHTTPServer((host, port), AiRpaRequestHandler)
    print(f"AI RPA app running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


INDEX_HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AI RPA Starter</title>
  <style>
    :root {
      color-scheme: light;
      --bg: #f6f7f9;
      --panel: #ffffff;
      --line: #d9dee7;
      --text: #1f2937;
      --muted: #687385;
      --accent: #0f766e;
      --accent-2: #9a3412;
      --danger: #b91c1c;
      --ok: #15803d;
      --code: #111827;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      font-size: 14px;
    }
    .app {
      display: grid;
      grid-template-columns: 260px minmax(420px, 1fr) 360px;
      height: 100vh;
      min-height: 680px;
    }
    aside, main, section {
      min-width: 0;
      min-height: 0;
    }
    aside {
      background: #111827;
      color: white;
      display: flex;
      flex-direction: column;
    }
    .brand {
      border-bottom: 1px solid rgba(255,255,255,.12);
      padding: 18px 18px 14px;
    }
    .brand h1 {
      font-size: 18px;
      line-height: 1.25;
      margin: 0 0 6px;
      font-weight: 760;
      letter-spacing: 0;
    }
    .brand p {
      color: rgba(255,255,255,.68);
      margin: 0;
      line-height: 1.45;
    }
    .workflow-list {
      overflow: auto;
      padding: 12px;
      display: grid;
      gap: 8px;
    }
    .workflow-button {
      background: transparent;
      border: 1px solid rgba(255,255,255,.14);
      color: white;
      cursor: pointer;
      min-height: 42px;
      padding: 10px 12px;
      text-align: left;
      width: 100%;
      border-radius: 8px;
    }
    .workflow-button.active {
      background: rgba(20, 184, 166, .18);
      border-color: rgba(45, 212, 191, .75);
    }
    main {
      display: grid;
      grid-template-rows: auto 1fr;
      border-right: 1px solid var(--line);
      background: var(--panel);
    }
    .toolbar {
      align-items: center;
      border-bottom: 1px solid var(--line);
      display: flex;
      gap: 8px;
      padding: 12px 14px;
      min-height: 62px;
    }
    button.command {
      border: 1px solid var(--line);
      background: white;
      color: var(--text);
      cursor: pointer;
      height: 36px;
      min-width: 78px;
      padding: 0 12px;
      border-radius: 8px;
      font-weight: 650;
    }
    button.command.primary {
      background: var(--accent);
      border-color: var(--accent);
      color: white;
    }
    button.command.warning {
      border-color: #fed7aa;
      color: var(--accent-2);
    }
    button.command:disabled {
      cursor: wait;
      opacity: .6;
    }
    .status {
      color: var(--muted);
      margin-left: auto;
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .editor-wrap {
      min-height: 0;
      padding: 14px;
    }
    textarea {
      background: var(--code);
      border: 1px solid #0b1020;
      color: #d1fae5;
      display: block;
      font: 13px/1.55 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      height: 100%;
      min-height: 0;
      outline: none;
      padding: 14px;
      resize: none;
      width: 100%;
      border-radius: 8px;
      tab-size: 2;
    }
    section.side {
      background: #fbfcfe;
      display: grid;
      grid-template-rows: auto 1fr auto;
    }
    .side-head {
      border-bottom: 1px solid var(--line);
      padding: 14px;
    }
    .side-head h2 {
      font-size: 15px;
      margin: 0 0 6px;
    }
    .settings {
      color: var(--muted);
      display: grid;
      gap: 5px;
      line-height: 1.45;
      margin-top: 10px;
    }
    .log {
      overflow: auto;
      padding: 12px 14px;
      display: grid;
      gap: 8px;
      align-content: start;
    }
    .log-item {
      border-left: 3px solid var(--line);
      padding: 6px 0 6px 10px;
      line-height: 1.45;
    }
    .log-item.pass { border-color: var(--ok); }
    .log-item.fail { border-color: var(--danger); }
    .log-title {
      font-weight: 720;
      margin-bottom: 2px;
    }
    .log-detail {
      color: var(--muted);
      overflow-wrap: anywhere;
    }
    .raw {
      border-top: 1px solid var(--line);
      max-height: 220px;
      overflow: auto;
      padding: 10px 14px;
      white-space: pre-wrap;
      color: #334155;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
    }
    @media (max-width: 980px) {
      .app {
        grid-template-columns: 1fr;
        grid-template-rows: auto minmax(520px, 1fr) minmax(320px, 46vh);
        height: auto;
        min-height: 100vh;
      }
      aside {
        max-height: 220px;
      }
      main {
        border-right: 0;
      }
    }
  </style>
</head>
<body>
  <div class="app">
    <aside>
      <div class="brand">
        <h1>AI RPA Starter</h1>
        <p>Workflow console</p>
      </div>
      <div id="workflowList" class="workflow-list"></div>
    </aside>
    <main>
      <div class="toolbar">
        <button id="saveBtn" class="command">保存</button>
        <button id="validateBtn" class="command">校验</button>
        <button id="runBtn" class="command primary">运行</button>
        <button id="reloadBtn" class="command warning">重载</button>
        <div id="status" class="status">准备就绪</div>
      </div>
      <div class="editor-wrap">
        <textarea id="editor" spellcheck="false"></textarea>
      </div>
    </main>
    <section class="side">
      <div class="side-head">
        <h2>运行信息</h2>
        <div id="settings" class="settings"></div>
      </div>
      <div id="log" class="log"></div>
      <pre id="raw" class="raw"></pre>
    </section>
  </div>
  <script>
    const state = { workflow: "", busy: false };
    const list = document.getElementById("workflowList");
    const editor = document.getElementById("editor");
    const statusEl = document.getElementById("status");
    const logEl = document.getElementById("log");
    const rawEl = document.getElementById("raw");
    const settingsEl = document.getElementById("settings");

    function setStatus(text) { statusEl.textContent = text; }
    function setBusy(value) {
      state.busy = value;
      for (const id of ["saveBtn", "validateBtn", "runBtn", "reloadBtn"]) {
        document.getElementById(id).disabled = value;
      }
    }
    async function api(path, options = {}) {
      const response = await fetch(path, {
        ...options,
        headers: { "content-type": "application/json", ...(options.headers || {}) }
      });
      const json = await response.json();
      if (!response.ok) throw json;
      return json;
    }
    function renderLog(result) {
      logEl.innerHTML = "";
      const steps = result.steps || [];
      for (const step of steps) {
        const item = document.createElement("div");
        item.className = "log-item " + (step.status || "");
        item.innerHTML = `<div class="log-title">${escapeHtml(step.title || step.node_id)}</div><div class="log-detail">${escapeHtml(step.detail || "")}</div>`;
        logEl.appendChild(item);
      }
      rawEl.textContent = JSON.stringify(result, null, 2);
    }
    function escapeHtml(value) {
      return String(value).replace(/[&<>"']/g, ch => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[ch]));
    }
    async function loadSettings() {
      const settings = await api("/api/settings");
      settingsEl.innerHTML = [
        `Mimo: ${settings.mimoConfigured ? "已配置" : "未配置"}`,
        `Headless: ${settings.headless}`,
        `Browser: ${settings.browserExecutable || "Playwright Chromium"}`,
        `Profile: ${settings.browserProfile}`
      ].map(escapeHtml).join("<br>");
    }
    async function loadWorkflows() {
      const data = await api("/api/workflows");
      list.innerHTML = "";
      for (const name of data.workflows) {
        const button = document.createElement("button");
        button.className = "workflow-button" + (name === state.workflow ? " active" : "");
        button.textContent = name;
        button.onclick = () => selectWorkflow(name);
        list.appendChild(button);
      }
      if (!state.workflow && data.workflows.length) await selectWorkflow(data.workflows[0]);
    }
    async function selectWorkflow(name) {
      state.workflow = name;
      setStatus(`加载 ${name}`);
      const data = await api(`/api/workflow?name=${encodeURIComponent(name)}`);
      editor.value = data.content;
      await loadWorkflows();
      setStatus(`${name} 已加载`);
    }
    async function saveWorkflow() {
      setBusy(true);
      try {
        const result = await api(`/api/workflow?name=${encodeURIComponent(state.workflow)}`, {
          method: "POST",
          body: JSON.stringify({ content: editor.value })
        });
        setStatus(result.message);
        rawEl.textContent = JSON.stringify(result, null, 2);
      } catch (error) {
        setStatus(error.error || "保存失败");
        rawEl.textContent = JSON.stringify(error, null, 2);
      } finally {
        setBusy(false);
      }
    }
    async function validateWorkflow() {
      setBusy(true);
      try {
        const result = await api("/api/validate", {
          method: "POST",
          body: JSON.stringify({ content: editor.value })
        });
        setStatus(result.message);
        rawEl.textContent = JSON.stringify(result, null, 2);
      } catch (error) {
        setStatus(error.error || "校验失败");
        rawEl.textContent = JSON.stringify(error, null, 2);
      } finally {
        setBusy(false);
      }
    }
    async function runWorkflow() {
      setBusy(true);
      logEl.innerHTML = "";
      rawEl.textContent = "";
      setStatus(`运行 ${state.workflow}`);
      try {
        const result = await api("/api/run", {
          method: "POST",
          body: JSON.stringify({ name: state.workflow, headless: false })
        });
        renderLog(result);
        setStatus(`运行完成：${result.status}`);
      } catch (error) {
        renderLog(error);
        setStatus(error.error || `运行失败：${error.status || "fail"}`);
      } finally {
        setBusy(false);
      }
    }
    document.getElementById("saveBtn").onclick = saveWorkflow;
    document.getElementById("validateBtn").onclick = validateWorkflow;
    document.getElementById("runBtn").onclick = runWorkflow;
    document.getElementById("reloadBtn").onclick = () => selectWorkflow(state.workflow);
    loadSettings().catch(error => setStatus(error.error || "设置加载失败"));
    loadWorkflows().catch(error => setStatus(error.error || "workflow 加载失败"));
  </script>
</body>
</html>"""


if __name__ == "__main__":
    main()
