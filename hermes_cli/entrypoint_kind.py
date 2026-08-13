"""Shared entry-point plugin classification for the ``hermes_agent.plugins`` group.

The ``hermes_agent.plugins`` entry-point group has TWO consumers that must
agree on who owns each entry point:

* ``hermes_cli.plugins.PluginManager`` — the general plugin manager. It must
  NOT import (and flag errored) pip providers that belong to their own
  discovery systems.
* ``providers/__init__.py`` — the model-provider registry. It must NOT invoke
  general plugins (which expose ``register(ctx)`` and belong to the manager)
  or memory providers (which belong to ``plugins/memory``) as if they were
  model providers.

One classifier, used by both, decides the ``kind`` so the two consumers can
never drift: a provider is loaded only by the registry, a memory provider only
by ``plugins/memory``, and a general plugin only by the manager. Kind inference
mirrors the directory-plugin heuristic (source markers), so a pip-installed
plugin is classified the same way a directory plugin of the same shape would
be — without importing the module (a pip provider pulling in onnxruntime via
fastembed costs ~60 MB RSS on every Hermes startup).

Kinds:
* ``standalone`` — a general plugin; the PluginManager's default (and the
  safe fallback on any resolution error). Not the provider registry's concern.
* ``exclusive`` — a memory provider (``register_memory_provider`` /
  ``MemoryProvider``); owned by ``plugins/memory`` discovery.
* ``model-provider`` — a model provider (``register_provider`` +
  ``ProviderProfile``); owned by ``providers/`` discovery.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# The shared entry-point group both consumers scan.
ENTRY_POINTS_GROUP = "hermes_agent.plugins"

_VALID_KINDS = ("standalone", "exclusive", "model-provider")


def detect_kind_from_source(source_text: str) -> Optional[str]:
    """Return the plugin kind implied by source markers, else ``None``.

    Mirrors ``plugins/memory/__init__.py:_is_memory_provider_dir``: a module
    that registers a memory provider (``register_memory_provider`` or
    ``MemoryProvider``) is ``exclusive``; a module that registers a model
    provider (``register_provider`` + ``ProviderProfile``) is
    ``model-provider``. Returns ``None`` when neither marker matches, so the
    caller can fall back to ``standalone``.
    """
    if "register_memory_provider" in source_text or "MemoryProvider" in source_text:
        return "exclusive"
    if "register_provider" in source_text and "ProviderProfile" in source_text:
        return "model-provider"
    return None


def read_source_from_origin(origin: Optional[str], limit: int = 8192) -> str:
    """Read the first ``limit`` chars of a module's source file.

    Returns ``""`` on any failure (callers fall back to ``standalone``).
    ``.pyc``/``.pyo`` origins are mapped back to their source path so source
    is still scanned when only the bytecode cache is present.
    """
    if not origin:
        return ""
    if origin.endswith((".pyc", ".pyo")):
        try:
            origin = importlib.util.source_from_cache(origin)
        except Exception:
            return ""
    if not origin.endswith(".py"):
        return ""
    try:
        return Path(origin).read_text(encoding="utf-8", errors="replace")[:limit]
    except Exception:
        return ""


def resolve_module_source(module_name: str, limit: int = 8192) -> str:
    """Return the first ``limit`` chars of a module's source WITHOUT importing.

    ``importlib.util.find_spec`` on a dotted name imports the parent package
    first (executing its ``__init__.py``), which would run arbitrary package
    initialization during discovery — paying the very import cost this
    classification exists to avoid. Only the top-level name is resolved with
    ``find_spec`` (import-free for top-level names); the remaining dotted
    segments are walked through ``submodule_search_locations`` by hand,
    mirroring the default PathFinder layout (``part.py`` module or
    ``part/__init__.py`` package). Namespace packages, zipped modules,
    extension modules, and anything else unexpected fall back to ``""``
    (→ ``standalone``, the safe default).
    """
    parts = [p for p in module_name.split(".") if p]
    if not parts:
        return ""
    try:
        spec = importlib.util.find_spec(parts[0])
        if spec is None or not spec.origin:
            return ""
        if len(parts) == 1:
            return read_source_from_origin(spec.origin, limit)
        search_paths = spec.submodule_search_locations
        if not search_paths:
            return ""
        search_root = Path(search_paths[0])
        for part in parts[1:]:
            as_module = search_root / f"{part}.py"
            if as_module.is_file():
                return read_source_from_origin(str(as_module), limit)
            as_pkg = search_root / part / "__init__.py"
            if as_pkg.is_file():
                return read_source_from_origin(str(as_pkg), limit)
            # Recurse into a subpackage's search locations.
            sub_spec = importlib.util.find_spec(part)
            if sub_spec is not None and sub_spec.submodule_search_locations:
                search_root = Path(sub_spec.submodule_search_locations[0])
            else:
                return ""
        return ""
    except Exception:
        return ""


def resolve_submodule_sources(module_name: str, limit: int = 8192) -> list:
    """Return the source of a package's direct submodules (without importing).

    Handles the thin-``__init__`` re-export case: a package whose entry-point
    module is ``__init__.py`` containing only ``from .core import register``
    while the actual ``register_provider(ProviderProfile(...))`` call lives in
    a submodule. Resolves the package's top-level location and reads each
    direct ``.py`` submodule (and nested ``__init__.py``), bounded, so a
    provider re-exported from a submodule is still classified. Returns ``[]``
    when the module is not a package or nothing readable is found. Never
    imports the package (import-free top-level resolution only).
    """
    parts = [p for p in module_name.split(".") if p]
    if not parts:
        return []
    out: list = []
    try:
        spec = importlib.util.find_spec(parts[0])
        if spec is None or not spec.submodule_search_locations:
            return []
        search_root = Path(spec.submodule_search_locations[0])
        if not search_root.is_dir():
            return []
        for child in sorted(search_root.iterdir()):
            if child.name.startswith(("_", ".")):
                continue
            if child.is_file() and child.suffix == ".py":
                src = read_source_from_origin(str(child), limit)
                if src:
                    out.append(src)
            elif child.is_dir() and (child / "__init__.py").is_file():
                src = read_source_from_origin(str(child / "__init__.py"), limit)
                if src:
                    out.append(src)
    except Exception:
        return []
    return out


def classify_entrypoint(ep):
    """Classify one entry point by its module source, without importing it.

    ``ep.value`` is ``module:func`` or ``module``. The module's source is read
    and classified via the shared marker heuristic.

    Return values:
    * ``"standalone"`` — the module source WAS read and it registers no
      memory/model provider → a genuine general plugin, owned by the manager.
    * ``"exclusive"`` — a memory provider; owned by ``plugins/memory``.
    * ``"model-provider"`` — a model provider; owned by ``providers/``.
    * ``"unknown"`` — the module source could not be resolved (non-Python
      target, namespace package, zip import, an entry point whose ``value`` is
      not a ``module:func`` string, a resolution error). The caller decides
      the safe fallback: the manager treats unknown as standalone; the
      provider registry falls through to its historical arity check so a real
      provider is never dropped just because its source could not be read.

    Never raises.
    """
    value = getattr(ep, "value", "") or ""
    if not isinstance(value, str) or ":" not in value:
        # A ``module:func`` target is the documented form. Anything else
        # (a bare callable, a module without a func, a non-string value)
        # cannot be source-classified here — report unknown so the caller
        # keeps its safe fallback instead of guessing.
        return "unknown"
    module_name = value.split(":", 1)[0].strip()
    if not module_name:
        return "unknown"
    source_text = resolve_module_source(module_name)
    if not source_text:
        # Source could not be read → we cannot rule out that this is a
        # provider. Not "standalone": that verdict must be earned by reading
        # the source and finding no provider markers.
        return "unknown"
    kind = detect_kind_from_source(source_text)
    if kind is not None:
        return kind
    # The entry-point module's own source had no provider markers, but it may
    # be a thin package that re-exports its register hook from a submodule
    # (e.g. ``__init__.py`` is just ``from .core import register`` while the
    # ``register_provider(ProviderProfile(...))`` call lives in ``core.py``).
    # Scan the package's submodules too so such a provider is classified
    # ``model-provider`` rather than ``standalone`` (which would silently
    # deregister a provider of the documented shape on main).
    sub = resolve_submodule_sources(module_name)
    for sub_source in sub:
        sub_kind = detect_kind_from_source(sub_source)
        if sub_kind is not None:
            return sub_kind
    return "standalone"


def kind_is_provider(kind: str) -> bool:
    """True when ``kind`` is owned by the provider registry (model-provider)."""
    return kind == "model-provider"


def kind_is_memory_provider(kind: str) -> bool:
    """True when ``kind`` is owned by ``plugins/memory`` discovery (exclusive)."""
    return kind == "exclusive"
