from __future__ import annotations

import os
import shutil
import socket
import threading
import time
from http.client import HTTPConnection
from pathlib import Path
from urllib.parse import urlparse

from .paths import APP_NAME, bundled_root, bundled_workflow_dir, user_data_root


def find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def prepare_desktop_data_dir() -> Path:
    data_dir = user_data_root()
    workflow_dir = data_dir / "workflows"
    workflow_dir.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("AI_RPA_WORKFLOW_DIR", str(workflow_dir))
    os.environ.setdefault("AI_RPA_BROWSER_PROFILE", str(data_dir / "browser-profile"))
    os.environ.setdefault("AI_RPA_SCREENSHOT_DIR", str(data_dir / "screenshots"))

    for source in bundled_workflow_dir().glob("*.json"):
        target = workflow_dir / source.name
        if not target.exists():
            shutil.copy2(source, target)
    env_example = bundled_root() / ".env.example"
    if env_example.exists():
        target_env_example = data_dir / ".env.example"
        if not target_env_example.exists():
            shutil.copy2(env_example, target_env_example)
    return data_dir


def wait_for_server(url: str, timeout: float = 15.0) -> None:
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        connection: HTTPConnection | None = None
        try:
            connection = HTTPConnection(parsed.hostname or "127.0.0.1", parsed.port or 80, timeout=1.0)
            connection.request("GET", path)
            response = connection.getresponse()
            response.read()
            if response.status < 500:
                return
        except Exception as error:
            last_error = error
        finally:
            if connection is not None:
                connection.close()
        time.sleep(0.15)
    raise RuntimeError(f"desktop server did not start: {last_error}")


def main() -> None:
    try:
        import webview
    except ModuleNotFoundError as error:
        raise RuntimeError("pywebview is required to run the desktop app. Install dependencies with pip install -r requirements.txt") from error

    prepare_desktop_data_dir()
    from .web_app import run_app

    port = find_free_port()
    url = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=run_app, args=("127.0.0.1", port), daemon=True)
    thread.start()
    wait_for_server(f"{url}/api/health")
    webview.create_window(APP_NAME, url, width=1440, height=920, min_size=(1100, 720))
    webview.start()


if __name__ == "__main__":
    main()
