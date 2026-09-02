"""End-to-end credential isolation proof for multiplex mode (Workstream A).

These exercise the REAL resolution path (runtime_provider, secret scope, MCP
interpolation) rather than mocking it, proving the property that matters: two
profiles with different keys never see each other's, and an unscoped read in
multiplex mode fails closed instead of leaking.
"""
import asyncio

import pytest

from pathlib import Path

from agent import secret_scope as ss


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    ss.set_multiplex_active(False)
    yield
    ss.set_multiplex_active(False)


class TestRuntimeProviderUsesScope:
    """hermes_cli.runtime_provider._getenv resolves through the secret scope."""


    def test_getenv_two_profiles_isolated(self, monkeypatch):
        from hermes_cli.runtime_provider import _getenv
        ss.set_multiplex_active(True)

        tok_a = ss.set_secret_scope({"OPENAI_API_KEY": "sk-A"})
        try:
            assert _getenv("OPENAI_API_KEY") == "sk-A"
        finally:
            ss.reset_secret_scope(tok_a)

        tok_b = ss.set_secret_scope({"OPENAI_API_KEY": "sk-B"})
        try:
            assert _getenv("OPENAI_API_KEY") == "sk-B"
        finally:
            ss.reset_secret_scope(tok_b)


class TestMcpInterpolationUsesScope:
    """MCP config ${VAR} interpolation resolves through the secret scope."""

    def test_interpolation_reads_scope(self, monkeypatch):
        from tools.mcp_tool import _interpolate_env_vars
        monkeypatch.setenv("MY_MCP_TOKEN", "global-token")
        ss.set_multiplex_active(True)
        tok = ss.set_secret_scope({"MY_MCP_TOKEN": "profile-token"})
        try:
            cfg = {"env": {"TOKEN": "${MY_MCP_TOKEN}"}}
            assert _interpolate_env_vars(cfg) == {"env": {"TOKEN": "profile-token"}}
        finally:
            ss.reset_secret_scope(tok)


class TestProfilePathResolutionUnderMultiplexScope:
    """Profile-scoped paths must follow the per-turn _profile_runtime_scope.

    The multiplexed gateway (gateway.multiplex_profiles) serves every profile
    from ONE process, scoping each inbound turn with _profile_runtime_scope —
    the same in-process-many-profiles topology as the desktop tui_gateway. The
    profile-isolation fixes (per-call path resolution + thread context
    propagation) must therefore hold under THIS scope too, not just desktop.
    This is the regression guard proving reachability is not desktop-only.
    """

    def _profiles(self, tmp_path):
        prof_a = tmp_path / "profA"
        prof_b = tmp_path / "profB"
        for p in (prof_a, prof_b):
            (p / "skills").mkdir(parents=True, exist_ok=True)
            (p / "state").mkdir(parents=True, exist_ok=True)
        return prof_a, prof_b

    def test_skills_dir_follows_multiplex_scope(self, tmp_path):
        from gateway.run import _profile_runtime_scope
        import tools.skills_hub as sh

        prof_a, prof_b = self._profiles(tmp_path)
        with _profile_runtime_scope(prof_a):
            a_seen = Path(sh.SKILLS_DIR)
        with _profile_runtime_scope(prof_b):
            b_seen = Path(sh.SKILLS_DIR)

        assert a_seen == prof_a / "skills"
        assert b_seen == prof_b / "skills"


def test_profile_scope_uses_prehydrated_external_source_without_global_env(
    tmp_path, monkeypatch
):
    """Startup hydration supplies one profile without mutating global env."""
    import os

    from agent.secret_sources.base import FetchResult
    from agent.secret_sources.registry import AppliedVar, ApplyReport, SourceReport
    from agent.secret_sources import registry
    from agent.secret_scope import get_secret
    from hermes_cli import env_loader
    from gateway.run import _profile_runtime_scope

    profile = tmp_path / "profiles" / "secondary"
    sibling = tmp_path / "profiles" / "sibling"
    profile.mkdir(parents=True)
    sibling.mkdir(parents=True)
    (profile / ".env").write_text(
        "EXPLICIT_API_KEY=dotenv-wins\n", encoding="utf-8"
    )
    monkeypatch.delenv("TEST_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("EXPLICIT_API_KEY", raising=False)
    monkeypatch.setattr(
        env_loader,
        "_load_secrets_config",
        lambda home: (
            {"fake-source": {"enabled": True}}
            if Path(home).resolve() == profile.resolve()
            else {}
        ),
    )

    calls = {"count": 0}

    def _fake_apply_all(_cfg, _home, *, environ=None):
        calls["count"] += 1
        assert environ is not os.environ
        assert environ is not None
        assert environ["EXPLICIT_API_KEY"] == "dotenv-wins"
        environ["TEST_PROVIDER_API_KEY"] = "profile-only"
        return ApplyReport(
            sources=[
                SourceReport(
                    name="fake-source",
                    label="Fake Source",
                    result=FetchResult(),
                    applied=["TEST_PROVIDER_API_KEY"],
                )
            ],
            provenance={
                "TEST_PROVIDER_API_KEY": AppliedVar(
                    name="TEST_PROVIDER_API_KEY",
                    source="fake-source",
                    shape="mapped",
                    overrode_env=False,
                )
            },
        )

    monkeypatch.setattr(registry, "apply_all", _fake_apply_all)
    env_loader.reset_secret_source_cache()

    # Scope-only: entering a cold profile's scope sees its .env but never
    # triggers the (blocking) external fetch.
    with _profile_runtime_scope(profile):
        assert get_secret("EXPLICIT_API_KEY") == "dotenv-wins"
        assert get_secret("TEST_PROVIDER_API_KEY") is None
    assert calls["count"] == 0

    # Explicit hydration — what secondary startup does off-loop.
    assert env_loader.hydrate_profile_secret_sources(profile) == {
        "TEST_PROVIDER_API_KEY": "profile-only"
    }
    assert calls["count"] == 1

    # From here on any hydration attempt inside the scope is a failure, even
    # one re-introduced via a function-local import.
    def _forbidden_hydrate(_home):
        raise AssertionError("_profile_runtime_scope must not hydrate")

    monkeypatch.setattr(
        env_loader, "hydrate_profile_secret_sources", _forbidden_hydrate
    )

    with _profile_runtime_scope(profile):
        assert get_secret("TEST_PROVIDER_API_KEY") == "profile-only"
        assert get_secret("EXPLICIT_API_KEY") == "dotenv-wins"
        assert env_loader.get_secret_source_values(profile) == {
            "TEST_PROVIDER_API_KEY": "profile-only"
        }
    with _profile_runtime_scope(profile):
        assert get_secret("TEST_PROVIDER_API_KEY") == "profile-only"
    with _profile_runtime_scope(sibling):
        assert get_secret("TEST_PROVIDER_API_KEY") is None

    assert calls["count"] == 1
    assert "TEST_PROVIDER_API_KEY" not in os.environ
    assert "EXPLICIT_API_KEY" not in os.environ


@pytest.mark.asyncio
async def test_secondary_startup_prehydrates_sequentially_off_loop(
    tmp_path, monkeypatch
):
    """Secondary startup hydrates each cold profile off-loop, one at a time,
    and only starts a profile's adapters after every hydration is done.

    Each fake hydration blocks its worker thread until the event loop has
    ticked a few more times. If hydration ran on the loop thread the ticker
    could never advance and the hydration would stall (flagged, not hung).
    """
    import threading
    import time
    from unittest.mock import MagicMock

    from agent.secret_scope import get_secret
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner, _profile_runtime_scope
    from hermes_cli import env_loader

    ticks = 0
    events: list = []
    stalled: list = []
    hydrated: dict[Path, dict[str, str]] = {}
    seen_credentials: dict[str, str | None] = {}
    lock = threading.Lock()

    def slow_hydrate(home):
        with lock:
            events.append(("hydrate-start", home.name, ticks))
        target = ticks + 3
        deadline = time.monotonic() + 0.5
        while ticks < target:
            if time.monotonic() > deadline:
                stalled.append(home.name)
                break
            time.sleep(0.001)
        with lock:
            hydrated[home.resolve()] = {
                "TEST_PROVIDER_API_KEY": f"{home.name}-credential"
            }
            events.append(("hydrate-end", home.name, ticks))
        return {}

    monkeypatch.setattr(
        "hermes_cli.env_loader.hydrate_profile_secret_sources", slow_hydrate
    )
    monkeypatch.setattr(
        env_loader,
        "get_secret_source_values",
        lambda home: dict(hydrated.get(Path(home).resolve(), {})),
    )

    homes = {
        name: tmp_path / name for name in ("default", "a", "b", "c")
    }
    for home in homes.values():
        home.mkdir()

    runner = GatewayRunner.__new__(GatewayRunner)
    runner.config = GatewayConfig(
        multiplex_profiles=True,
        multiplex_profile_allowlist=["a", "b", "c"],
    )
    runner.adapters = {}
    runner._profile_adapters = {}
    runner.pairing_stores = {n: MagicMock() for n in ("default", "a", "b", "c")}
    runner.pairing_store = runner.pairing_stores["default"]

    async def fake_start_one(profile_name, profile_home, claimed):
        events.append(("start", profile_name, ticks))
        with _profile_runtime_scope(profile_home):
            seen_credentials[profile_name] = get_secret("TEST_PROVIDER_API_KEY")
        runner._profile_adapters[profile_name] = {}
        return 1

    monkeypatch.setattr(runner, "_start_one_profile_adapters", fake_start_one)
    monkeypatch.setattr(
        "hermes_cli.profiles.profiles_to_serve",
        lambda multiplex, profile_allowlist=None: [
            ("default", homes["default"]),
            ("a", homes["a"]),
            ("b", homes["b"]),
            ("c", homes["c"]),
        ],
    )
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("gateway.status.write_runtime_status", lambda **kwargs: None)

    async def ticker():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    ticker_task = asyncio.create_task(ticker())
    try:
        connected = await runner._start_secondary_profile_adapters()
    finally:
        ticker_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker_task

    assert connected == 3
    assert stalled == [], f"event loop stalled during hydration of {stalled}"

    hydrate_events = [(kind, name) for kind, name, _ in events if kind != "start"]
    # Sequential: start/end pairs never interleave; active profile skipped.
    assert hydrate_events == [
        ("hydrate-start", "a"), ("hydrate-end", "a"),
        ("hydrate-start", "b"), ("hydrate-end", "b"),
        ("hydrate-start", "c"), ("hydrate-end", "c"),
    ]
    # Loop kept ticking while each hydration blocked its thread.
    by_name = {(kind, name): tick for kind, name, tick in events}
    for name in ("a", "b", "c"):
        assert by_name[("hydrate-end", name)] > by_name[("hydrate-start", name)]
    # Every hydration finishes before any profile starts; starts stay ordered.
    kinds = [kind for kind, _, _ in events]
    assert kinds.index("start") > max(
        i for i, k in enumerate(kinds) if k == "hydrate-end"
    )
    assert [name for kind, name, _ in events if kind == "start"] == ["a", "b", "c"]
    assert seen_credentials == {
        "a": "a-credential",
        "b": "b-credential",
        "c": "c-credential",
    }


