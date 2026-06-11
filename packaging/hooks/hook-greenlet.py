from PyInstaller.utils.hooks import collect_dynamic_libs


hiddenimports = [
    "greenlet._greenlet",
    "greenlet.platform",
]

binaries = collect_dynamic_libs("greenlet", search_patterns=["*.dll", "*.pyd", "*.dylib", "lib*.so"])
