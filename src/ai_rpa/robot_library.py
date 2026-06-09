from __future__ import annotations

import asyncio
import json
from dataclasses import asdict

from .executor import PlaywrightWorkflowExecutor
from .workflow_loader import load_workflow


def load_env_if_available() -> None:
    try:
        from dotenv import load_dotenv
    except ModuleNotFoundError:
        return
    load_dotenv()


class AiRpaLibrary:
    def run_workflow(self, workflow_path: str) -> str:
        load_env_if_available()
        workflow = load_workflow(workflow_path)
        result = asyncio.run(PlaywrightWorkflowExecutor().run(workflow))
        payload = json.dumps(asdict(result), ensure_ascii=False, indent=2)
        if result.status == "fail":
            raise AssertionError(payload)
        return payload
