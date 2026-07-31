"""Behavior-contract + E2E tests for the multi memory-provider CONSUMER rewiring
(upstream #5688).

The config-layer setter/resolver contracts live in
``test_memory_provider_config.py``. THIS file locks the consumer seams that the
dual review flagged as the open data-loss defect — every consumer must see the
SAME active set the canonical setter wrote, and the manager must not
double-register a provider:

- ``backup.py._collect_memory_provider_external_paths`` collects paths for ALL
  active providers (the data-loss regression: singular-only read backed up ZERO
  paths when 2+ providers were active).
- ``plugins/memory._get_active_memory_providers`` / plugins_cmd plural readers
  resolve the ordered list (legacy singular fallback).
- ``MemoryManager.add_provider`` de-dupes by name, order-preserving.

These are behavior contracts (assert the consumer↔setter invariant), not
change-detector snapshots. The E2E cases exercise the real resolution chain
against a temp ``HERMES_HOME`` with a real on-disk ``config.yaml``.
"""
import sys
import types

import pytest


# --------------------------------------------------------------------------- #
# E2E: real config.yaml under a temp HERMES_HOME, real resolver chain.
# --------------------------------------------------------------------------- #
@pytest.fixture
def temp_hermes_home(tmp_path, monkeypatch):
    """Point HERMES_HOME at a temp dir with a real config.yaml and clear the
    config load cache so each test reads its own on-disk config."""
    home = tmp_path / "hermes_home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    # Drop any process-level config cache so our on-disk writes are read fresh.
    import hermes_cli.config as cfgmod
    if hasattr(cfgmod, "_CONFIG_CACHE"):
        try:
            cfgmod._CONFIG_CACHE.clear()  # type: ignore[attr-defined]
        except Exception:
            pass
    return home


def _write_config(home, yaml_text):
    (home / "config.yaml").write_text(yaml_text, encoding="utf-8")
    import hermes_cli.config as cfgmod
    if hasattr(cfgmod, "_CONFIG_CACHE"):
        try:
            cfgmod._CONFIG_CACHE.clear()  # type: ignore[attr-defined]
        except Exception:
            pass


class TestPluginsMemoryPluralReader:
    def test_reads_ordered_list(self, temp_hermes_home):
        _write_config(temp_hermes_home,
                      "memory:\n  providers:\n    - honcho\n    - hindsight\n")
        from plugins.memory import _get_active_memory_providers
        assert _get_active_memory_providers() == ["honcho", "hindsight"]

    def test_legacy_singular_fallback(self, temp_hermes_home):
        _write_config(temp_hermes_home, "memory:\n  provider: honcho\n")
        from plugins.memory import _get_active_memory_providers, _get_active_memory_provider
        assert _get_active_memory_providers() == ["honcho"]
        # Singular shim returns the first of the resolved set.
        assert _get_active_memory_provider() == "honcho"

    def test_singular_shim_returns_first_of_many(self, temp_hermes_home):
        _write_config(temp_hermes_home,
                      "memory:\n  providers:\n    - honcho\n    - hindsight\n")
        from plugins.memory import _get_active_memory_provider
        # The shim intentionally exposes only the first — callers needing all
        # must use the plural fn. This asserts the documented shim contract.
        assert _get_active_memory_provider() == "honcho"

    def test_no_provider_returns_empty(self, temp_hermes_home):
        _write_config(temp_hermes_home, "model:\n  default: x\n")
        from plugins.memory import _get_active_memory_providers, _get_active_memory_provider
        assert _get_active_memory_providers() == []
        assert _get_active_memory_provider() is None


class TestBackupCollectsAllProviders:
    """The data-loss fix: backup must collect external paths for EVERY active
    provider, not just the singular field (which the setter blanks at 2+)."""

    def _install_fake_providers(self, monkeypatch, tmp_path, active_names, paths_by_name):
        # Fake load_memory_provider + _get_active_memory_providers so the test
        # exercises backup's ITERATION contract without real plugin SDKs.
        import hermes_cli.backup as backup

        class _FakeProvider:
            def __init__(self, name, paths):
                self.name = name
                self._paths = paths
            def backup_paths(self):
                return self._paths

        made = {}
        for name, ps in paths_by_name.items():
            real = []
            for rel in ps:
                p = tmp_path / rel
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x")
                real.append(str(p))
            made[name] = _FakeProvider(name, real)

        fake_mod = types.ModuleType("plugins.memory")
        fake_mod._get_active_memory_providers = lambda: list(active_names)
        fake_mod.load_memory_provider = lambda n: made.get(n)
        monkeypatch.setitem(sys.modules, "plugins.memory", fake_mod)
        return backup

    def test_collects_paths_from_two_providers(self, monkeypatch, tmp_path):
        backup = self._install_fake_providers(
            monkeypatch, tmp_path,
            active_names=["honcho", "hindsight"],
            paths_by_name={"honcho": ["honcho/a.db"], "hindsight": ["hindsight/b.db"]},
        )
        out = backup._collect_memory_provider_external_paths()
        names = {p.name for p in out}
        assert names == {"a.db", "b.db"}, "backup must collect BOTH providers' paths"

    def test_single_provider_still_collected(self, monkeypatch, tmp_path):
        backup = self._install_fake_providers(
            monkeypatch, tmp_path,
            active_names=["honcho"],
            paths_by_name={"honcho": ["honcho/a.db"]},
        )
        out = backup._collect_memory_provider_external_paths()
        assert {p.name for p in out} == {"a.db"}

    def test_no_providers_returns_empty(self, monkeypatch, tmp_path):
        backup = self._install_fake_providers(
            monkeypatch, tmp_path, active_names=[], paths_by_name={},
        )
        assert backup._collect_memory_provider_external_paths() == []

    def test_overlapping_paths_deduped(self, monkeypatch, tmp_path):
        backup = self._install_fake_providers(
            monkeypatch, tmp_path,
            active_names=["honcho", "hindsight"],
            paths_by_name={"honcho": ["shared/x.db"], "hindsight": ["shared/x.db"]},
        )
        out = backup._collect_memory_provider_external_paths()
        assert len(out) == 1, "the same resolved path must appear once"

    def test_one_flaky_provider_does_not_drop_the_other(self, monkeypatch, tmp_path):
        import hermes_cli.backup as backup

        class _GoodProvider:
            name = "hindsight"
            def backup_paths(self):
                p = tmp_path / "hindsight" / "b.db"
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("x")
                return [str(p)]

        class _FlakyProvider:
            name = "honcho"
            def backup_paths(self):
                raise RuntimeError("provider IO blew up")

        made = {"honcho": _FlakyProvider(), "hindsight": _GoodProvider()}
        fake_mod = types.ModuleType("plugins.memory")
        fake_mod._get_active_memory_providers = lambda: ["honcho", "hindsight"]
        fake_mod.load_memory_provider = lambda n: made.get(n)
        monkeypatch.setitem(sys.modules, "plugins.memory", fake_mod)

        out = backup._collect_memory_provider_external_paths()
        # The flaky provider is skipped but the healthy one's path survives.
        assert {p.name for p in out} == {"b.db"}


class TestMemoryManagerDedup:
    """Q2 amendment: add_provider de-dupes by name, order-preserving."""

    def _mk_manager_and_provider(self):
        from agent.memory_manager import MemoryManager
        from agent.memory_provider import MemoryProvider

        class _P(MemoryProvider):
            def __init__(self, name):
                self._n = name
            @property
            def name(self):
                return self._n
            def is_available(self):
                return True
            def initialize(self, session_id, **kwargs):
                return None
            def get_tool_schemas(self):
                return []
            def handle_tool_call(self, tool_name, args, **kwargs):
                return ""
        return MemoryManager, _P

    def test_duplicate_name_skipped(self):
        MemoryManager, _P = self._mk_manager_and_provider()
        m = MemoryManager()
        m.add_provider(_P("honcho"))
        m.add_provider(_P("honcho"))  # duplicate
        assert m.provider_names == ["honcho"], "second same-name registration must be skipped"

    def test_order_preserved_first_wins(self):
        MemoryManager, _P = self._mk_manager_and_provider()
        m = MemoryManager()
        m.add_provider(_P("honcho"))
        m.add_provider(_P("hindsight"))
        m.add_provider(_P("honcho"))  # dup of the first
        assert m.provider_names == ["honcho", "hindsight"], \
            "dup must not reorder or append; first registration keeps its slot"

    def test_distinct_providers_all_registered(self):
        MemoryManager, _P = self._mk_manager_and_provider()
        m = MemoryManager()
        m.add_provider(_P("honcho"))
        m.add_provider(_P("hindsight"))
        assert m.provider_names == ["honcho", "hindsight"]


class TestDisplayConsumersReportAllActive:
    """#5688 broken-window sweep: STATUS/DIAGNOSTIC/DUMP surfaces must not
    under-report active providers in multi-provider mode. A trusted surface
    that shows only the singular provider (blanked at 2+ by the setter) LIES
    about system state. Behavior contract: every active provider is reported.
    """

    def test_dump_reports_all_active_providers(self):
        from hermes_cli.dump import _memory_provider

        # Multi: ordered list governs, singular blank (canonical setter shape).
        assert _memory_provider(
            {"memory": {"providers": ["honcho", "mem0"], "provider": ""}}
        ) == "honcho, mem0"
        # Single legacy fallback still works.
        assert _memory_provider({"memory": {"provider": "mem0"}}) == "mem0"
        # Empty => built-in.
        assert _memory_provider({"memory": {"providers": [], "provider": ""}}) == "built-in"
        assert _memory_provider({}) == "built-in"

    def test_discover_flags_every_missing_active_provider(self, temp_hermes_home, monkeypatch):
        """web_server.discover_memory_providers must flag a missing 2nd/3rd
        active provider, not just the singular one."""
        import hermes_cli.web_server as ws

        _write_config(
            temp_hermes_home,
            "memory:\n  providers:\n    - ghost_a\n    - ghost_b\n",
        )
        # No plugins discovered on disk => both actives are "configured but
        # missing". The old singular read would flag at most one.
        import plugins.memory as _plugins_memory
        monkeypatch.setattr(_plugins_memory, "discover_memory_providers", lambda: [])
        # Stub the per-row enrichment so we only assess the missing-map logic.
        monkeypatch.setattr(ws, "_load_memory_provider", lambda name: None)
        monkeypatch.setattr(ws, "_memory_provider_setup_info", lambda name: {})
        monkeypatch.setattr(ws, "_memory_provider_is_configured", lambda name, provider: False)
        monkeypatch.setattr(ws, "_normalize_memory_provider_schema", lambda name, provider: [])

        rows = ws._discover_memory_provider_statuses()
        by_name = {r["name"]: r for r in rows}
        assert by_name["ghost_a"]["status"] == "missing"
        assert by_name["ghost_b"]["status"] == "missing"
