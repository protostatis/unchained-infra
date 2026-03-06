"""Resolve the active web module instance for extracted handlers.

When the service is launched via ``python web.py`` the runtime module is
``__main__``. Importing ``web`` from extracted modules would create a second
module instance with split global state. This loader prefers ``__main__`` when
it is the running ``web.py`` module, and falls back to importing ``web``.
"""

from __future__ import annotations

import importlib
import os
import sys
from types import ModuleType


def _is_runtime_web_main(module: ModuleType | None) -> bool:
    if module is None:
        return False
    module_file = getattr(module, "__file__", "")
    if not module_file:
        return False
    if os.path.basename(module_file) != "web.py":
        return False
    # Require known web runtime symbols to avoid false matches.
    return hasattr(module, "_auth") and hasattr(module, "create_session_token")


def get_core() -> ModuleType:
    """Return the active ``web.py`` module object for shared runtime state."""
    main_mod = sys.modules.get("__main__")
    if _is_runtime_web_main(main_mod):
        return main_mod

    web_mod = sys.modules.get("web")
    if web_mod is not None:
        return web_mod

    return importlib.import_module("web")
