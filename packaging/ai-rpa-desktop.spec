# -*- mode: python ; coding: utf-8 -*-

from pathlib import Path
import sys


SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR.parent


def include_dir(name: str) -> tuple[str, str] | None:
    path = ROOT / name
    if path.exists():
        return (str(path), name)
    return None


def include_file(name: str) -> tuple[str, str] | None:
    path = ROOT / name
    if path.exists():
        return (str(path), ".")
    return None


datas = [item for item in [
    include_dir("static"),
    include_dir("workflows"),
    include_dir("fixtures"),
    include_dir("prompts"),
    include_file(".env.example"),
] if item is not None]


a = Analysis(
    [str(SPEC_DIR / "desktop_entry.py")],
    pathex=[str(ROOT / "src")],
    binaries=[],
    datas=datas,
    hiddenimports=["webview"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AI RPA Starter",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AI RPA Starter",
)

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="AI RPA Starter.app",
        icon=None,
        bundle_identifier="com.ai-rpa-starter.desktop",
    )
