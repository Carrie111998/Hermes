"""End-to-end credential isolation proof for multiplex mode (Workstream A).

These exercise the REAL resolution path (runtime_provider, secret scope, MCP
interpolation) rather than mocking it, proving the property that matters: two
profiles with different keys never see each other's, and an unscoped read in
multiplex mode fails closed instead of leaking.
"""
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


def test_cold_profile_hydrates_external_source_without_global_env(
    tmp_path, monkeypatch
):
    """The first routed secondary turn must resolve its own source locally."""
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

    def _fake_apply_all(_cfg, _home, *, environ=None, isolated_environment=False):
        assert isolated_environment is True
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


def test_cold_profile_onepassword_uses_profile_op_env_without_global_leak(
    tmp_path, monkeypatch
):
    """A first routed profile gets its own .op.env bootstrap token locally."""
    import os
    from types import SimpleNamespace

    import agent.secret_sources.onepassword as onepassword
    from agent.secret_scope import get_secret
    from hermes_cli import env_loader
    from gateway.run import _profile_runtime_scope

    profile = tmp_path / "profiles" / "secondary"
    profile.mkdir(parents=True)
    (profile / ".env").write_text(
        "EXPLICIT_API_KEY=dotenv-wins\n", encoding="utf-8"
    )
    (profile / ".op.env").write_text(
        "OP_SERVICE_ACCOUNT_TOKEN=profile-bootstrap\n", encoding="utf-8"
    )
    (profile / "config.yaml").write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    env:\n"
        "      TEST_PROVIDER_API_KEY: op://Vault/Item/credential\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN", raising=False)
    monkeypatch.delenv("TEST_PROVIDER_API_KEY", raising=False)
    monkeypatch.delenv("EXPLICIT_API_KEY", raising=False)
    monkeypatch.setattr(onepassword, "find_op", lambda _path: Path("/fake/op"))

    child_env: dict[str, str] = {}

    def _fake_run(_command, **kwargs):
        child_env.update(kwargs["env"])
        return SimpleNamespace(
            returncode=0, stdout="profile-provider-key\n", stderr=""
        )

    monkeypatch.setattr(onepassword.subprocess, "run", _fake_run)
    env_loader.reset_secret_source_cache()
    onepassword._reset_cache_for_tests(profile)

    with _profile_runtime_scope(profile):
        assert get_secret("TEST_PROVIDER_API_KEY") == "profile-provider-key"
        assert get_secret("EXPLICIT_API_KEY") == "dotenv-wins"

    assert child_env["OP_SERVICE_ACCOUNT_TOKEN"] == "profile-bootstrap"

    # Match the normal loader's precedence: a profile .env token is explicit
    # and therefore wins over the .op.env bootstrap fallback.
    (profile / ".env").write_text(
        "OP_SERVICE_ACCOUNT_TOKEN=dotenv-bootstrap\n"
        "EXPLICIT_API_KEY=dotenv-wins\n",
        encoding="utf-8",
    )
    child_env.clear()
    env_loader.reset_secret_source_cache()
    onepassword._reset_cache_for_tests(profile)

    with _profile_runtime_scope(profile):
        assert get_secret("TEST_PROVIDER_API_KEY") == "profile-provider-key"

    assert child_env["OP_SERVICE_ACCOUNT_TOKEN"] == "dotenv-bootstrap"
    assert "TEST_PROVIDER_API_KEY" not in child_env
    assert "OP_SERVICE_ACCOUNT_TOKEN" not in os.environ
    assert "TEST_PROVIDER_API_KEY" not in os.environ
    assert "EXPLICIT_API_KEY" not in os.environ


