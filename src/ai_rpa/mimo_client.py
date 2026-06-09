from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any


class MimoClient:
    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("MIMO_API_KEY", "")
        self.base_url = (base_url or os.getenv("MIMO_BASE_URL", "https://api.xiaomimimo.com/v1")).rstrip("/")
        self.model = model or os.getenv("MIMO_MODEL", "mimo-v2.5")

    def complete_json(
        self,
        system: str,
        user: str,
        screenshot_path: str | None = None,
        attached_image_data_url: str | None = None,
        retries: int = 3,
        timeout: int = 60,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("MIMO_API_KEY is not configured")
        content: list[dict[str, Any]] = [{"type": "text", "text": user}]
        if screenshot_path:
            content.append({"type": "image_url", "image_url": {"url": image_data_url(screenshot_path)}})
        if attached_image_data_url:
            content.append({"type": "image_url", "image_url": {"url": attached_image_data_url}})
        last_error: Exception | None = None
        import requests

        for attempt in range(max(1, retries)):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                    json={
                        "model": self.model,
                        "messages": [
                            {"role": "system", "content": system},
                            {"role": "user", "content": content},
                        ],
                        "response_format": {"type": "json_object"},
                        "max_completion_tokens": 1200,
                    },
                    timeout=timeout,
                )
                response.raise_for_status()
                text = response.json()["choices"][0]["message"]["content"]
                if not str(text or "").strip():
                    raise RuntimeError("Mimo returned an empty response")
                return json.loads(text)
            except Exception as error:
                last_error = error
                if attempt < retries - 1:
                    time.sleep(0.4 * (attempt + 1))
        raise RuntimeError(f"Mimo request failed after {retries} attempts: {last_error}") from last_error

    def health_check(self, timeout: int = 15) -> tuple[bool, str]:
        if not self.api_key:
            return False, "MIMO_API_KEY is not configured"
        import requests

        try:
            response = requests.post(
                f"{self.base_url}/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": "Return compact JSON."},
                        {"role": "user", "content": [{"type": "text", "text": "Respond with {\"ok\":true}"}]},
                    ],
                    "response_format": {"type": "json_object"},
                    "max_completion_tokens": 32,
                },
                timeout=timeout,
            )
            response.raise_for_status()
            text = response.json()["choices"][0]["message"]["content"]
            parsed = json.loads(text)
            if isinstance(parsed, dict):
                return True, ""
            return False, "Mimo health check returned invalid JSON object"
        except Exception as error:
            return False, f"Mimo health check failed: {error}"


def image_data_url(path: str) -> str:
    image_path = Path(path)
    data = base64.b64encode(image_path.read_bytes()).decode("ascii")
    suffix = image_path.suffix.lower()
    mime = "image/jpeg" if suffix in {".jpg", ".jpeg"} else "image/png"
    return f"data:{mime};base64,{data}"
