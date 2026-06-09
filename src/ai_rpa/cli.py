from __future__ import annotations

import argparse
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


def main() -> None:
    load_env_if_available()
    parser = argparse.ArgumentParser(description="Run an AI RPA workflow")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("workflow")
    app_parser = subparsers.add_parser("app")
    app_parser.add_argument("--host", default="127.0.0.1")
    app_parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.command == "run":
        workflow = load_workflow(args.workflow)
        result = asyncio.run(PlaywrightWorkflowExecutor().run(workflow))
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    if args.command == "app":
        from .web_app import run_app
        run_app(args.host, args.port)


if __name__ == "__main__":
    main()
