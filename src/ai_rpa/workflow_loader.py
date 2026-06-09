from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import Workflow, WorkflowNode


SUPPORTED_NODE_TYPES = {
    "web.open",
    "web.scroll",
    "web.hover",
    "web.click",
    "web.input",
    "web.wait_for",
    "web.select",
    "web.extract",
    "web.close_modals",
    "web.close_tab",
    "ai.ask",
    "flow.wait",
    "flow.if",
    "flow.else",
    "flow.end_if",
    "flow.loop",
    "flow.end_loop",
}


class WorkflowValidationError(ValueError):
    pass


def load_workflow(path: str | Path) -> Workflow:
    source_path = Path(path)
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    return parse_workflow(raw)


def parse_workflow(raw: dict[str, Any]) -> Workflow:
    workflow_id = require_string(raw, "id")
    name = require_string(raw, "name")
    version = str(raw.get("version") or "0.1.0")
    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
      raise WorkflowValidationError("workflow.nodes must be a non-empty list")
    page_objects = raw.get("pageObjects") or raw.get("page_objects") or {}
    if not isinstance(page_objects, dict):
        raise WorkflowValidationError("workflow.pageObjects must be an object")

    nodes = [parse_node(item, index) for index, item in enumerate(raw_nodes)]
    seen_ids: set[str] = set()
    for node in nodes:
        if node.id in seen_ids:
            raise WorkflowValidationError(f"duplicate node id: {node.id}")
        seen_ids.add(node.id)

    return Workflow(id=workflow_id, name=name, version=version, nodes=nodes, pageObjects=page_objects)


def parse_node(raw: Any, index: int) -> WorkflowNode:
    if not isinstance(raw, dict):
        raise WorkflowValidationError(f"node[{index}] must be an object")
    node_id = require_string(raw, "id")
    node_type = require_string(raw, "type")
    if node_type not in SUPPORTED_NODE_TYPES:
        raise WorkflowValidationError(f"unsupported node type: {node_type}")
    title = str(raw.get("title") or node_id)
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise WorkflowValidationError(f"node[{node_id}].params must be an object")
    return WorkflowNode(
        id=node_id,
        type=node_type,
        title=title,
        params=params,
        disabled=bool(raw.get("disabled", False)),
    )


def require_string(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise WorkflowValidationError(f"missing required string: {key}")
    return value.strip()
