"""Stable import surface for the usage-meter plugin (hyphen package path).

The bundled plugin lives at ``plugins/usage-meter/`` (directory name with a
hyphen). Python cannot import that path as a regular package name, so the
tui_gateway RPC methods and tests load the plugin modules through this thin
loader instead of depending on plugin-manager discovery.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any, Dict, Optional


def _load() -> ModuleType:
    key = "_hermes_usage_meter_plugin"
    existing = sys.modules.get(key)
    if existing is not None:
        return existing

    root = Path(__file__).resolve().parent / "usage-meter"
    # Load package root first so relative imports inside the plugin resolve.
    init_path = root / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        key,
        init_path,
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load usage-meter plugin from {init_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[key] = module
    # Pre-register submodules under the package for relative imports.
    for sub in ("ledger", "capture"):
        sub_path = root / f"{sub}.py"
        sub_key = f"{key}.{sub}"
        sub_spec = importlib.util.spec_from_file_location(sub_key, sub_path)
        if sub_spec is None or sub_spec.loader is None:
            continue
        sub_mod = importlib.util.module_from_spec(sub_spec)
        sys.modules[sub_key] = sub_mod
        # Make attribute available before exec for circular safety.
        setattr(module, sub, sub_mod)
        sub_spec.loader.exec_module(sub_mod)
    spec.loader.exec_module(module)
    return module


def meter_summary(*, tz_name: Optional[str] = None) -> Dict[str, Any]:
    return _load().meter_summary(tz_name=tz_name)


def meter_details(*, scope: str = "month", tz_name: Optional[str] = None) -> Dict[str, Any]:
    return _load().meter_details(scope=scope, tz_name=tz_name)


def meter_recent(*, limit: int = 50) -> Dict[str, Any]:
    return _load().meter_recent(limit=limit)


def on_post_api_request(**kwargs: Any) -> None:
    return _load().capture.on_post_api_request(**kwargs)
