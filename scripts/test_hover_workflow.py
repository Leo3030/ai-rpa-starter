from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from ai_rpa.executor import PlaywrightWorkflowExecutor
from ai_rpa.models import Workflow, WorkflowNode


HTML = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <style>
      body { margin: 0; height: 1800px; font-family: sans-serif; }
      #target {
        margin-top: 1200px;
        width: 160px;
        height: 120px;
        background: #2f7fd1;
        color: white;
        display: grid;
        place-items: center;
      }
      #status { position: fixed; top: 0; left: 0; background: white; }
    </style>
  </head>
  <body>
    <div id="status">idle</div>
    <div id="target">hover me</div>
    <script>
      const target = document.querySelector("#target");
      const status = document.querySelector("#status");
      for (const eventName of ["pointerover", "mouseover", "mouseenter"]) {
        target.addEventListener(eventName, () => {
          status.textContent = `hovered:${eventName}:scrollY=${window.scrollY}`;
        });
      }
    </script>
  </body>
</html>
"""


async def run_case(name: str, html_path: Path, profile_dir: Path, no_scroll: bool) -> None:
    os.environ["AI_RPA_KEEP_BROWSER_OPEN"] = "false"
    os.environ["AI_RPA_MODAL_GUARD"] = "false"
    os.environ["AI_RPA_BROWSER_PROFILE"] = str(profile_dir)

    workflow = Workflow(
        id=f"hover-{name}",
        name=f"Hover {name}",
        version="0.1.0",
        nodes=[
            WorkflowNode(
                id="open",
                type="web.open",
                title="打开 hover 测试页",
                params={"url": str(html_path)},
            ),
            WorkflowNode(
                id="hover",
                type="web.hover",
                title="悬停测试元素",
                params={"selector": "#target", "target": "测试元素", "noScroll": no_scroll},
            ),
            WorkflowNode(
                id="wait-hovered",
                type="web.wait_for",
                title="等待 hover 状态",
                params={"text": "hovered"},
            ),
        ],
    )
    result = await PlaywrightWorkflowExecutor(headless=True).run(workflow)
    print(f"[{name}] status={result.status}")
    for step in result.steps:
        print(f"[{name}] {step.status} {step.node_id}: {step.detail}")
    if result.status != "pass":
        raise SystemExit(1)


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="ai-rpa-hover-") as tmp:
        root = Path(tmp)
        html_path = root / "hover.html"
        html_path.write_text(HTML, encoding="utf-8")
        await run_case("normal", html_path, root / "profile-normal", no_scroll=False)
        await run_case("no-scroll", html_path, root / "profile-no-scroll", no_scroll=True)


if __name__ == "__main__":
    asyncio.run(main())
