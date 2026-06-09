from __future__ import annotations

import os
import platform
import sys
from pathlib import Path


APP_NAME = "AI RPA Starter"


def bundled_root() -> Path:
    """Return the source root or PyInstaller's extracted resource root."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass).resolve()
    return Path(__file__).resolve().parents[2]


def user_data_root() -> Path:
    override = os.getenv("AI_RPA_USER_DATA_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()

    system = platform.system()
    home = Path.home()
    if system == "Windows":
        base = Path(os.getenv("APPDATA", home / "AppData" / "Roaming"))
        return (base / APP_NAME).resolve()
    if system == "Darwin":
        return (home / "Library" / "Application Support" / APP_NAME).resolve()
    return (Path(os.getenv("XDG_DATA_HOME", home / ".local" / "share")) / "ai-rpa-starter").resolve()


def static_dir() -> Path:
    override = os.getenv("AI_RPA_STATIC_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return bundled_root() / "static"


def bundled_workflow_dir() -> Path:
    return bundled_root() / "workflows"


def workflow_dir() -> Path:
    override = os.getenv("AI_RPA_WORKFLOW_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return bundled_workflow_dir()


def env_file_candidates() -> list[Path]:
    override = os.getenv("AI_RPA_ENV_FILE", "").strip()
    if override:
        return [Path(override).expanduser().resolve()]
    return [
        Path.cwd() / ".env",
        user_data_root() / ".env",
        bundled_root() / ".env",
    ]
