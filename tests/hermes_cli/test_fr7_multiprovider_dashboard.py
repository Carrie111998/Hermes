"""FR-7 (#5688) — dashboard multi-memory-provider save-path + schema tests.

The load-bearing regression these guard against: the dashboard save path used
to accept a single ``provider`` string and write it through
``set_active_memory_providers([provider])``, silently clobbering an ordered
``memory.providers`` list of 2+ down to one entry. FR-7 makes the PUT accept
the full ordered list and round-trip it without loss.

These exercise the REAL FastAPI endpoint + real config read/write against the
per-test isolated HERMES_HOME (the ``_isolate_hermes_home`` autouse fixture),
not a mock — the only stub is ``_require_memory_provider_ready`` (readiness
depends on plugins actually installed on disk, orthogonal to the save-path
contract under test).
"""

from __future__ import annotations

import pytest

from hermes_cli.config import get_active_memory_providers, load_config, save_config


@pytest.fixture
def client(monkeypatch):
    try:
        from starlette.testclient import TestClient
    except ImportError:  # pragma: no cover - env without starlette
        pytest.skip("fastapi/starlette not installed")

    from hermes_cli import web_server
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME

    # Readiness is a disk/plugin-install concern, not the save-path contract.
    monkeypatch.setattr(web_server, "_require_memory_provider_ready", lambda name: None)

    c = TestClient(app)
    # An earlier test in the suite may flip the shared ``app.state.auth_required``
    # gate on and leave it on (the dashboard_auth middleware then demands a
    # session cookie and 401s every request with reason "no_cookie"). Force the
    # loopback/no-auth posture for this client so the save-path contract under
    # test isn't hostage to global app-state pollution.
    app.state.auth_required = False
    # Read the session token LIVE off the module, not a value captured at import
    # time: an earlier test may rotate the global via ``_apply_ssh_session_token``.
    c.headers[_SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return c


class TestMemoryProviderSavePath:
    """PUT /api/memory/provider must preserve the full ordered list."""

    def test_put_list_round_trips_all_rows(self, client):
        """2 providers in → 2 providers out, never clobbered to 1.

        This is the exact regression FR-7 exists to kill.
        """
        resp = client.put("/api/memory/provider", json={"providers": ["honcho", "mem0"]})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is True
        assert body["providers"] == ["honcho", "mem0"]

        # Verified at source: config on disk, read through the FR-1 resolver.
        assert get_active_memory_providers(load_config()) == ["honcho", "mem0"]

    def test_put_list_preserves_priority_order(self, client):
        """List order == priority; the saved order matches the sent order."""
        client.put("/api/memory/provider", json={"providers": ["mem0", "honcho", "hindsight"]})
        assert get_active_memory_providers(load_config()) == ["mem0", "honcho", "hindsight"]

        # Reversing the intent must persist the reversed order, not merge.
        client.put("/api/memory/provider", json={"providers": ["hindsight", "honcho", "mem0"]})
        assert get_active_memory_providers(load_config()) == ["hindsight", "honcho", "mem0"]

    def test_put_shrink_to_one_does_not_leave_stale_entries(self, client):
        """Going 2 → 1 must drop the removed provider, not keep the old list."""
        client.put("/api/memory/provider", json={"providers": ["honcho", "mem0"]})
        client.put("/api/memory/provider", json={"providers": ["honcho"]})
        assert get_active_memory_providers(load_config()) == ["honcho"]
        # Legacy singular mirror is set when exactly one is active.
        assert load_config()["memory"]["provider"] == "honcho"

    def test_put_empty_list_disables_all_providers(self, client):
        """Empty list → built-in only; both list and legacy field cleared."""
        client.put("/api/memory/provider", json={"providers": ["honcho", "mem0"]})
        resp = client.put("/api/memory/provider", json={"providers": []})
        assert resp.status_code == 200
        assert get_active_memory_providers(load_config()) == []
        assert load_config()["memory"]["provider"] == ""

    def test_put_dedups_order_preservingly(self, client):
        """A duplicate slug collapses to first occurrence, order preserved."""
        client.put("/api/memory/provider", json={"providers": ["honcho", "mem0", "honcho"]})
        assert get_active_memory_providers(load_config()) == ["honcho", "mem0"]

    def test_put_drops_builtin_sentinels_and_blanks(self, client):
        """'built-in'/''/whitespace are not real providers and are dropped."""
        client.put(
            "/api/memory/provider",
            json={"providers": ["honcho", "built-in", "", "  ", "mem0"]},
        )
        assert get_active_memory_providers(load_config()) == ["honcho", "mem0"]

    def test_legacy_singular_body_still_works(self, client):
        """An older dashboard sending {provider: x} must still activate x."""
        resp = client.put("/api/memory/provider", json={"provider": "honcho"})
        assert resp.status_code == 200
        assert get_active_memory_providers(load_config()) == ["honcho"]

    def test_legacy_singular_empty_disables(self, client):
        """Older dashboard sending {provider: ''} disables (built-in only)."""
        client.put("/api/memory/provider", json={"providers": ["honcho"]})
        resp = client.put("/api/memory/provider", json={"provider": ""})
        assert resp.status_code == 200
        assert get_active_memory_providers(load_config()) == []

    def test_list_wins_over_singular_when_both_sent(self, client):
        """When both fields arrive, the ordered list is canonical."""
        client.put(
            "/api/memory/provider",
            json={"providers": ["honcho", "mem0"], "provider": "hindsight"},
        )
        assert get_active_memory_providers(load_config()) == ["honcho", "mem0"]


class TestMemoryProvidersSchema:
    """The schema surfaces memory.providers as a list-bound editor field."""

    def test_schema_emits_memory_providers_list_field(self):
        from hermes_cli.web_server import CONFIG_SCHEMA

        entry = CONFIG_SCHEMA.get("memory.providers")
        assert entry is not None, "memory.providers must be a schema field for the editor to render"
        assert entry["type"] == "list"
        assert "options" in entry

    def test_schema_options_preserve_configured_offdisk_provider(self):
        """A configured provider removed from disk must stay selectable."""
        from hermes_cli.web_server import _memory_provider_schema_options

        cfg = {"memory": {"providers": ["honcho", "gone_from_disk"]}}
        options = _memory_provider_schema_options(cfg)
        assert "gone_from_disk" in options, (
            "silent-drop regression: a configured-but-uninstalled provider "
            "vanished from the schema options (the UI data-loss class FR-7 kills)"
        )

    def test_dynamic_merge_recomputes_providers_options(self, monkeypatch):
        """Per-request merge re-discovers providers for the list field too."""
        from hermes_cli import web_server

        monkeypatch.setattr(web_server, "load_config", lambda: {"memory": {"providers": ["honcho"]}})
        monkeypatch.setattr(
            web_server,
            "_memory_provider_options",
            lambda: ["", "honcho", "freshly_installed"],
        )

        fields = web_server._schema_with_dynamic_provider_options()
        assert "freshly_installed" in fields["memory.providers"]["options"]
        assert fields["memory.providers"]["type"] == "list"


class TestMemoryStatusReportsList:
    """GET /api/memory reports the full active list, not just the first."""

    def test_status_reports_active_providers_list(self, client):
        client.put("/api/memory/provider", json={"providers": ["honcho", "mem0"]})
        body = client.get("/api/memory").json()
        assert body["active_providers"] == ["honcho", "mem0"]
        # Legacy singular ``active`` remains the first (back-compat).
        assert body["active"] == "honcho"


class TestProviderConfigValuesEditDoesNotClobberActivation:
    """FINDING 2 (#5688): saving a provider's connection VALUES must route the
    activation write through the canonical setter and must NOT clobber an
    existing multi-provider active list.

    The old code wrote ``memory_config["provider"] = name`` directly (a
    var-alias write that bypassed the setter), which for a multi-provider user
    set singular=name while the plural list still governed — a writer≠reader
    split identical to the original backup.py incident.
    """

    def test_values_edit_preserves_existing_multiprovider_list(self, monkeypatch, tmp_path):
        from hermes_cli import web_server
        from hermes_cli.config import (
            get_active_memory_providers,
            load_config,
            save_config,
            set_active_memory_providers,
        )

        # Two providers already active, honcho highest priority.
        cfg = load_config()
        set_active_memory_providers(cfg, ["honcho", "mem0"])
        save_config(cfg)

        # Editing mem0's connection values must NOT reorder or drop honcho,
        # and must NOT leave singular=mem0 while the plural list still governs.
        monkeypatch.setattr(web_server, "_write_provider_flat", lambda provider, values: None)
        monkeypatch.setattr(web_server, "_write_provider_honcho", lambda provider, values: None)

        class _Schema:
            name = "mem0"
            storage = "flat"

        web_server._update_memory_provider_config(_Schema(), {"api_key": "x"})

        # honcho still first, mem0 still present, order intact, no clobber.
        assert get_active_memory_providers(load_config()) == ["honcho", "mem0"]

    def test_values_edit_activates_a_not_yet_active_provider(self, monkeypatch, tmp_path):
        from hermes_cli import web_server
        from hermes_cli.config import get_active_memory_providers, load_config, save_config, set_active_memory_providers

        cfg = load_config()
        set_active_memory_providers(cfg, ["honcho"])
        save_config(cfg)

        monkeypatch.setattr(web_server, "_write_provider_flat", lambda provider, values: None)
        monkeypatch.setattr(web_server, "_write_provider_honcho", lambda provider, values: None)

        class _Schema:
            name = "mem0"
            storage = "flat"

        web_server._update_memory_provider_config(_Schema(), {"api_key": "x"})

        # Configuring a new provider appends it (activate-on-configure UX),
        # preserving the existing one's priority.
        assert get_active_memory_providers(load_config()) == ["honcho", "mem0"]

    def test_non_declared_arm_preserves_multiprovider_list(self, client, monkeypatch):
        """FINDING 3 (#5688): the surface!=declared arm of the SAME PUT handler
        (the _load_memory_provider path) also raw-wrote memory.provider=name,
        bypassing the setter. A multi-provider user hitting this arm got
        singular=name while the plural list still governed — identical split to
        finding 2, in the other arm of the same handler.

        Exercises the REAL endpoint with surface omitted (=> non-declared arm);
        only the disk/plugin-dependent bits are stubbed.
        """
        from hermes_cli import web_server

        cfg = load_config()
        web_server.set_active_memory_providers(cfg, ["honcho", "mem0"])
        save_config(cfg)

        monkeypatch.setattr(web_server, "_load_memory_provider", lambda name: object())
        monkeypatch.setattr(
            web_server, "_write_memory_provider_config_values", lambda name, provider, values: None
        )
        monkeypatch.setattr(web_server, "_require_memory_provider_ready", lambda name: None)

        # Hit mem0's config with NO surface param => non-declared arm.
        resp = client.put("/api/memory/providers/mem0/config", json={"values": {"api_key": "x"}})
        assert resp.status_code == 200

        # honcho still first, mem0 still present, order intact, no clobber.
        assert get_active_memory_providers(load_config()) == ["honcho", "mem0"]

    def test_non_declared_arm_activates_new_provider(self, client, monkeypatch):
        """New provider via the non-declared arm appends, preserving priority."""
        from hermes_cli import web_server

        cfg = load_config()
        web_server.set_active_memory_providers(cfg, ["honcho"])
        save_config(cfg)

        monkeypatch.setattr(web_server, "_load_memory_provider", lambda name: object())
        monkeypatch.setattr(
            web_server, "_write_memory_provider_config_values", lambda name, provider, values: None
        )
        monkeypatch.setattr(web_server, "_require_memory_provider_ready", lambda name: None)

        resp = client.put("/api/memory/providers/mem0/config", json={"values": {"api_key": "x"}})
        assert resp.status_code == 200
        assert get_active_memory_providers(load_config()) == ["honcho", "mem0"]
