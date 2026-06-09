from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkflowNode:
    id: str
    type: str
    title: str
    params: dict[str, Any] = field(default_factory=dict)
    disabled: bool = False


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    version: str
    nodes: list[WorkflowNode]
    pageObjects: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunStep:
    node_id: str
    title: str
    status: str
    detail: str
