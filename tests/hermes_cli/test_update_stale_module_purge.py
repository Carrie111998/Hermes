"""Tests for _purge_stale_hermes_modules — the class fix for stale
sys.modules breaking the gateway auto-restart after `hermes update`.

Field failure (2026-08-20, Teknium's Linux box): `hermes update` pulled a
checkout where hermes_cli/gateway.py newly imports `line_input` from
hermes_cli.cli_output, but the updater process had cli_output cached from
before that symbol existed. The function-level `from hermes_cli.gateway
import ...` in the restart phase raised ImportError, the whole phase
aborted, and the running gateway kept serving pre-update code.

The old mitigation (_UPDATE_RUNTIME_RELOAD_MODULES) reloaded 3 hardcoded
modules — re-fixed per symptom. The purge evicts EVERY cached module under
the Hermes package prefixes so later imports rebuild a self-consistent
module graph from the updated checkout.
"""

from __future__ import annotations

import importlib
import json
import sys
import types

import pytest

from hermes_cli import main as cli_main
from hermes_cli import update_cmd


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot & restore sys.modules around each test.

    The purge under test evicts real Hermes modules from the cache; later
    tests in the same process may hold references to the evicted module
    objects (e.g. `patch.object` targets), so put the originals back.
    """
    snapshot = dict(sys.modules)
    yield
    for name, mod in snapshot.items():
        sys.modules[name] = mod
    for name in list(sys.modules):
        if name not in snapshot:
            del sys.modules[name]
    # A post-purge import also rebinds the submodule attribute on its parent
    # package (``hermes_cli.config`` -> fresh module object). Put those back
    # too: pytest's monkeypatch resolves dotted targets through package
    # attributes, so a stale binding would make a later test patch a module
    # object that no ``from hermes_cli.config import ...`` ever sees.
    for name, mod in snapshot.items():
        parent, _, child = name.rpartition(".")
        if not parent or parent not in snapshot:
            continue
        bound = getattr(snapshot[parent], child, None)
        if isinstance(bound, types.ModuleType) and bound is not mod:
            setattr(snapshot[parent], child, mod)


def _fake_module(name: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__stale_sentinel__ = True
    return mod


def test_purge_evicts_hermes_prefixed_modules():
    victims = [
        "hermes_cli.cli_output",
        "hermes_cli.gateway",
        "gateway.status",
        "tools.ansi_strip",
        "tui_gateway.server",
        "agent.memory_store",
    ]
    added = []
    for name in victims:
        if name not in sys.modules:
            sys.modules[name] = _fake_module(name)
            added.append(name)
    try:
        cli_main._purge_stale_hermes_modules()
        for name in victims:
            mod = sys.modules.get(name)
            assert mod is None or not getattr(mod, "__stale_sentinel__", False), (
                f"{name} survived the purge"
            )
    finally:
        for name in added:
            sys.modules.pop(name, None)


def test_purge_protects_executing_modules():
    # The updater's own modules must survive — they're running this code.
    cli_main._purge_stale_hermes_modules()
    assert sys.modules.get("hermes_cli.update_cmd") is update_cmd
    assert sys.modules.get("hermes_cli.main") is cli_main
    assert "hermes_cli" in sys.modules


def test_purge_leaves_prefix_lookalikes_alone():
    # `gateway_foo` starts with the string prefix "gateway" but is NOT the
    # gateway package — the root-segment check must spare it.
    lookalikes = ["gatewayd", "toolshed", "agents_external"]
    added = []
    for name in lookalikes:
        if name not in sys.modules:
            sys.modules[name] = _fake_module(name)
            added.append(name)
    try:
        cli_main._purge_stale_hermes_modules()
        for name in lookalikes:
            assert name in sys.modules, f"{name} was wrongly purged"
    finally:
        for name in added:
            sys.modules.pop(name, None)


def test_purge_never_raises_on_weird_sys_modules():
    # Entries with None values (import machinery quirk) must not break it.
    sys.modules["hermes_cli._purge_test_none"] = None  # type: ignore[assignment]
    try:
        cli_main._purge_stale_hermes_modules()
    finally:
        sys.modules.pop("hermes_cli._purge_test_none", None)


def test_stale_symbol_scenario_end_to_end():
    """Reproduce the field failure shape: a cached module missing a symbol
    that freshly-imported code needs — purge, then re-import resolves it."""
    name = "hermes_cli.cli_output"
    real = sys.modules.get(name)
    # Install a stale stand-in WITHOUT line_input (pre-d0132b582 world).
    stale = types.ModuleType(name)
    sys.modules[name] = stale
    try:
        # The failure mode: importing the symbol from the stale cache dies.
        try:
            from hermes_cli.cli_output import line_input  # noqa: F401
            raised = False
        except ImportError:
            raised = True
        assert raised, "precondition: stale module must lack line_input"

        cli_main._purge_stale_hermes_modules()

        # Post-purge, the import resolves against real on-disk source.
        from hermes_cli.cli_output import line_input  # noqa: F401
    finally:
        sys.modules.pop(name, None)
        if real is not None:
            sys.modules[name] = real


def test_purge_keeps_in_flight_update_receipt(tmp_path, monkeypatch):
    """The receipt begun before the checkout changed must be the one the
    success path finalizes after the purge.

    ``hermes_cli.update_receipt`` keeps the open receipt as a module-level
    singleton. If the purge evicted it, the lazy
    ``from hermes_cli.update_receipt import finalize_update_receipt`` that
    follows the gateway-restart phase would bind a fresh module with no open
    receipt and return None — so every successful update would finish
    without a receipt on disk (the Desktop's managed SSH update then reports
    the run as failed for want of a durable receipt).
    """
    import hermes_cli.update_receipt as ur

    home = tmp_path / ".hermes"
    home.mkdir()
    # Env, not attribute patching: the purge re-imports hermes_cli.config,
    # and a fresh module would not carry a monkeypatched attribute.
    monkeypatch.setenv("HERMES_HOME", str(home))
    ur._current = None
    ur.begin_update_receipt()
    assert ur._current is not None
    try:
        cli_main._purge_stale_hermes_modules()

        reimported = importlib.import_module("hermes_cli.update_receipt")
        assert reimported is ur, "update_receipt was evicted by the purge"
        assert reimported._current is not None

        path = reimported.finalize_update_receipt("success")
        assert path is not None and path.exists()
        assert path.is_relative_to(home)
        assert json.loads(path.read_text(encoding="utf-8"))["outcome"] == "success"
    finally:
        ur._current = None
