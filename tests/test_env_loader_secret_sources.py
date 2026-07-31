"""Tests for the secret-source tracking in ``hermes_cli.env_loader``.

These cover the small public surface that lets `hermes model` / `hermes setup`
label detected credentials with their origin ("from Bitwarden") so users
don't see an unexplained "credentials ✓" line when their .env is empty.
"""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from hermes_cli import env_loader  # noqa: E402


_TEST_HOME = Path("/tmp/hermes-secret-source-test-home")


def _record_source(name: str, source: str, home: Path = _TEST_HOME) -> None:
    env_loader._SECRET_SOURCES_BY_HOME.setdefault(str(home.resolve()), {})[name] = source


@pytest.fixture(autouse=True)
def _reset_sources():
    """Each test starts with a clean source map and applied-home guard."""
    env_loader._SECRET_SOURCES_BY_HOME.clear()
    env_loader._SECRET_SOURCE_VALUES_BY_HOME.clear()
    env_loader.reset_secret_source_cache()
    yield
    env_loader._SECRET_SOURCES_BY_HOME.clear()
    env_loader._SECRET_SOURCE_VALUES_BY_HOME.clear()
    env_loader.reset_secret_source_cache()


def test_get_secret_source_returns_none_for_untracked_var():
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") is None


def test_get_secret_source_returns_label_for_tracked_var():
    _record_source("ANTHROPIC_API_KEY", "bitwarden")
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"


def test_get_secret_source_is_scoped_by_resolved_home(tmp_path):
    """Two profiles may attribute the same key to different source backends."""
    home_a = tmp_path / "alpha"
    home_b = tmp_path / "beta"
    _record_source("SHARED_API_KEY", "bitwarden", home_a)
    _record_source("SHARED_API_KEY", "onepassword", home_b)

    assert env_loader.get_secret_source("SHARED_API_KEY", home_a) == "bitwarden"
    assert env_loader.get_secret_source("SHARED_API_KEY", home_b) == "onepassword"
    assert env_loader.format_secret_source_suffix("SHARED_API_KEY", home_a) == " (from Bitwarden)"
    assert env_loader.format_secret_source_suffix("SHARED_API_KEY", home_b) == " (from 1Password)"
    assert env_loader.get_secret_source("SHARED_API_KEY") is None


def test_get_secret_source_values_returns_home_snapshot_copy(tmp_path):
    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    home_a.mkdir()
    home_b.mkdir()

    env_loader._SECRET_SOURCE_VALUES_BY_HOME[str(home_a.resolve())] = {
        "ANTHROPIC_API_KEY": "sk-profile-a"
    }

    snapshot = env_loader.get_secret_source_values(home_a)
    assert snapshot == {
        "ANTHROPIC_API_KEY": "sk-profile-a"
    }
    assert env_loader.get_secret_source_values(home_b) == {}
    snapshot["ANTHROPIC_API_KEY"] = "mutated"
    assert env_loader.get_secret_source_values(home_a) == {
        "ANTHROPIC_API_KEY": "sk-profile-a"
    }


def test_format_secret_source_suffix_empty_for_untracked():
    # Credentials from .env or the shell shouldn't add noise — the
    # implicit case stays unlabeled.
    assert env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY") == ""


def test_format_secret_source_suffix_bitwarden_uses_proper_name():
    _record_source("ANTHROPIC_API_KEY", "bitwarden")
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from Bitwarden)"
    )


def test_format_secret_source_suffix_generic_label_for_future_sources():
    # Future-proofing: a new secret source (e.g. "vault") should still
    # produce a sensible label without needing to edit every call site.
    _record_source("OPENAI_API_KEY", "vault")
    assert (
        env_loader.format_secret_source_suffix("OPENAI_API_KEY")
        == " (from vault)"
    )


def test_format_secret_source_suffix_onepassword_uses_proper_name():
    _record_source("OPENAI_API_KEY", "onepassword")
    assert (
        env_loader.format_secret_source_suffix("OPENAI_API_KEY")
        == " (from 1Password)"
    )


def test_apply_external_secret_sources_records_bitwarden_origin(tmp_path, monkeypatch):
    """End-to-end: when the Bitwarden source fetches keys, applied vars
    end up in ``_SECRET_SOURCES`` so the UI can label them."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    # Stub the fetch layer under the SecretSource adapter.
    import agent.secret_sources.bitwarden as bw_module

    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))
    monkeypatch.setattr(
        bw_module,
        "fetch_bitwarden_secrets",
        lambda **_kw: ({"ANTHROPIC_API_KEY": "sk-ant-test"}, []),
    )

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from Bitwarden)"
    )


def test_cold_profile_bitwarden_uses_profile_bootstrap_without_global_env(
    tmp_path, monkeypatch
):
    """Real Bitwarden adapter reads its token from the profile-local view."""
    monkeypatch.delenv("BWS_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / ".env").write_text(
        "BWS_ACCESS_TOKEN=profile-bootstrap\n", encoding="utf-8"
    )
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    import agent.secret_sources.bitwarden as bw_module
    from agent.secret_sources import registry as reg_module

    captured = {}
    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))

    def _fake_fetch(**kwargs):
        captured.update(kwargs)
        return {"ANTHROPIC_API_KEY": "profile-provider-key"}, []

    monkeypatch.setattr(bw_module, "fetch_bitwarden_secrets", _fake_fetch)
    reg_module._reset_registry_for_tests()

    assert env_loader.hydrate_profile_secret_sources(tmp_path) == {
        "ANTHROPIC_API_KEY": "profile-provider-key"
    }
    assert captured["access_token"] == "profile-bootstrap"
    assert os.environ.get("BWS_ACCESS_TOKEN") is None
    assert os.environ.get("ANTHROPIC_API_KEY") is None


def test_apply_external_secret_sources_noop_when_disabled(tmp_path, monkeypatch):
    """Disabled Bitwarden config must not touch the source map."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") is None


def test_apply_external_secret_sources_dedupes_within_process(tmp_path, monkeypatch):
    """``load_hermes_dotenv()`` is called at module-import time from several
    hot modules (cli.py, hermes_cli/main.py, run_agent.py, ...).  The
    Bitwarden status line previously printed once per call — 3-5x per
    startup.  The applied-home guard must short-circuit subsequent calls
    so the heavy work (config re-parse, Bitwarden lookup, status print)
    runs exactly once per HERMES_HOME per process.
    """

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    call_count = {"n": 0}
    def _fake_fetch(**_kwargs):
        call_count["n"] += 1
        return {"ANTHROPIC_API_KEY": "sk-ant-test"}, []

    import agent.secret_sources.bitwarden as bw_module
    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))
    monkeypatch.setattr(bw_module, "fetch_bitwarden_secrets", _fake_fetch)

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    # Five calls in a row, simulating module-import-time invocations from
    # cli.py, hermes_cli/main.py, run_agent.py, trajectory_compressor.py,
    # gateway/run.py.  Only the first should actually call the backend.
    for _ in range(5):
        env_loader._apply_external_secret_sources(tmp_path)

    assert call_count["n"] == 1, (
        "Bitwarden backend was called {} time(s); expected exactly 1 — "
        "the applied-home guard is broken.".format(call_count["n"])
    )

    # Source tracking still works after dedup.
    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "bitwarden"
    assert env_loader.get_secret_source_values(tmp_path) == {
        "ANTHROPIC_API_KEY": "sk-ant-test"
    }

    # reset_secret_source_cache() forces a fresh pull on the next call.
    env_loader.reset_secret_source_cache()
    env_loader._apply_external_secret_sources(tmp_path)
    assert call_count["n"] == 2


def test_apply_external_secret_sources_status_line_suppresses_secret_names(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("BWS_ACCESS_TOKEN", "0.test-token")
    monkeypatch.delenv("LEAK_THIS_API_KEY", raising=False)
    monkeypatch.delenv("LEAK_THIS_TOKEN", raising=False)
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden:\n"
        "    enabled: true\n"
        "    project_id: test-project\n"
        "    access_token_env: BWS_ACCESS_TOKEN\n",
        encoding="utf-8",
    )

    import agent.secret_sources.bitwarden as bw_module

    monkeypatch.setattr(bw_module, "find_bws", lambda **_kw: Path("/fake/bws"))
    monkeypatch.setattr(
        bw_module,
        "fetch_bitwarden_secrets",
        lambda **_kw: (
            {"LEAK_THIS_API_KEY": "sk-test", "LEAK_THIS_TOKEN": "tok-test"},
            [],
        ),
    )

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    err = capsys.readouterr().err
    assert "Bitwarden Secrets Manager: applied 2 secrets" in err
    assert "LEAK_THIS_API_KEY" not in err
    assert "LEAK_THIS_TOKEN" not in err


def test_external_secret_values_are_isolated_between_homes(tmp_path, monkeypatch):
    """A later apply for the same key must not mutate an earlier home snapshot."""
    from agent.secret_scope import build_profile_secret_scope
    from agent.secret_sources.base import FetchResult
    from agent.secret_sources.registry import (
        AppliedVar,
        ApplyReport,
        SourceReport,
    )
    from agent.secret_sources import registry as reg_module

    home_a = tmp_path / "profile-a"
    home_b = tmp_path / "profile-b"
    for home in (home_a, home_b):
        home.mkdir()
        (home / "config.yaml").write_text(
            "secrets:\n  test-source:\n    enabled: true\n",
            encoding="utf-8",
        )

    values = {
        str(home_a.resolve()): "value-a",
        str(home_b.resolve()): "value-b",
    }

    def _fake_apply_all(_cfg, home_path):
        value = values[str(Path(home_path).resolve())]
        monkeypatch.setenv("SHARED_API_KEY", value)
        return ApplyReport(
            # Real apply_all always appends a SourceReport per enabled
            # source; the env_loader guard (#40597) early-returns on an
            # empty sources list, so the fake must match the real shape.
            sources=[
                SourceReport(
                    name="test-source",
                    label="Test Source",
                    result=FetchResult(),
                    applied=["SHARED_API_KEY"],
                )
            ],
            provenance={
                "SHARED_API_KEY": AppliedVar(
                    name="SHARED_API_KEY",
                    source="test-source",
                    shape="mapped",
                    overrode_env=True,
                )
            }
        )

    monkeypatch.setattr(reg_module, "apply_all", _fake_apply_all)

    env_loader._apply_external_secret_sources(home_a)
    env_loader._apply_external_secret_sources(home_b)

    assert os.environ["SHARED_API_KEY"] == "value-b"
    assert env_loader.get_secret_source_values(home_a) == {
        "SHARED_API_KEY": "value-a"
    }
    assert env_loader.get_secret_source_values(home_b) == {
        "SHARED_API_KEY": "value-b"
    }
    assert build_profile_secret_scope(home_a) == {
        "SHARED_API_KEY": "value-a"
    }
    assert build_profile_secret_scope(home_b) == {
        "SHARED_API_KEY": "value-b"
    }


def test_apply_external_secret_sources_records_onepassword_origin(tmp_path, monkeypatch):
    """When the 1Password source resolves refs, applied vars end up in
    ``_SECRET_SOURCES`` labeled ``onepassword``."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    env:\n"
        "      ANTHROPIC_API_KEY: 'op://Private/Anthropic/credential'\n",
        encoding="utf-8",
    )

    import agent.secret_sources.onepassword as op_module

    monkeypatch.setattr(op_module, "find_op", lambda *_a, **_kw: Path("/fake/op"))
    monkeypatch.setattr(
        op_module,
        "fetch_onepassword_secrets",
        lambda **_kw: ({"ANTHROPIC_API_KEY": "sk-ant-test"}, []),
    )

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    assert env_loader.get_secret_source("ANTHROPIC_API_KEY") == "onepassword"
    assert (
        env_loader.format_secret_source_suffix("ANTHROPIC_API_KEY")
        == " (from 1Password)"
    )


def test_apply_external_secret_sources_survives_non_dict_section(tmp_path, monkeypatch):
    """A malformed `secrets:` section must not abort startup (fail-open).

    Both `onepassword: true` (non-dict) and a bad bitwarden section must be
    coerced to empty config instead of raising AttributeError up through
    load_hermes_dotenv().
    """

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  bitwarden: true\n"
        "  onepassword: true\n",
        encoding="utf-8",
    )

    # Must not raise and must not record anything.
    env_loader._apply_external_secret_sources(tmp_path)
    assert env_loader.get_secret_source("ANYTHING") is None


def test_apply_external_secret_sources_bad_ttl_does_not_crash(tmp_path, monkeypatch):
    """A non-numeric cache_ttl_seconds must be coerced, not crash startup."""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "secrets:\n"
        "  onepassword:\n"
        "    enabled: true\n"
        "    cache_ttl_seconds: not-a-number\n"
        "    env:\n"
        "      K: 'op://V/I/F'\n",
        encoding="utf-8",
    )

    captured = {}

    def _fake_fetch(**kwargs):
        captured.update(kwargs)
        return {}, []

    import agent.secret_sources.onepassword as op_module
    monkeypatch.setattr(op_module, "find_op", lambda *_a, **_kw: Path("/fake/op"))
    monkeypatch.setattr(op_module, "fetch_onepassword_secrets", _fake_fetch)

    from agent.secret_sources import registry as reg_module

    reg_module._reset_registry_for_tests()

    env_loader._apply_external_secret_sources(tmp_path)

    # Coerced to the 300s default rather than raising ValueError.
    assert captured["cache_ttl_seconds"] == 300


def test_cold_profile_hydration_allows_distinct_homes_to_fetch_concurrently(
    tmp_path, monkeypatch
):
    """A slow source for one cold profile must not stall another profile."""
    from types import SimpleNamespace

    from agent.secret_sources import registry

    homes = [tmp_path / "alpha", tmp_path / "beta"]
    for home in homes:
        home.mkdir()
    monkeypatch.setattr(
        env_loader, "_load_secrets_config", lambda _home: {"fake": {"enabled": True}}
    )
    fetch_barrier = threading.Barrier(2, timeout=2)

    def _fake_apply_all(_cfg, home, *, environ, isolated_environment=False):
        assert isolated_environment is True
        fetch_barrier.wait()
        key = f"{home.name.upper()}_API_KEY"
        environ[key] = f"{home.name}-only"
        return SimpleNamespace(
            sources=[SimpleNamespace()],
            provenance={key: SimpleNamespace(source="fake")},
        )

    monkeypatch.setattr(registry, "apply_all", _fake_apply_all)
    results: dict[str, dict[str, str]] = {}
    errors: list[BaseException] = []

    def _hydrate(home):
        try:
            results[home.name] = env_loader.hydrate_profile_secret_sources(home)
        except BaseException as exc:  # test thread must report failures
            errors.append(exc)

    workers = [threading.Thread(target=_hydrate, args=(home,)) for home in homes]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=5)

    assert not errors
    assert all(not worker.is_alive() for worker in workers)
    assert results == {
        "alpha": {"ALPHA_API_KEY": "alpha-only"},
        "beta": {"BETA_API_KEY": "beta-only"},
    }


def test_reset_during_cold_profile_hydration_retries_for_a_complete_snapshot(
    tmp_path, monkeypatch
):
    """A reset cannot leave a home marked applied with no snapshot."""
    from types import SimpleNamespace

    from agent.secret_sources import registry

    home = tmp_path / "secondary"
    home.mkdir()
    monkeypatch.setattr(
        env_loader, "_load_secrets_config", lambda _home: {"fake": {"enabled": True}}
    )
    first_fetch_started = threading.Event()
    release_first_fetch = threading.Event()
    calls = {"count": 0}

    def _fake_apply_all(_cfg, _home, *, environ, isolated_environment=False):
        assert isolated_environment is True
        calls["count"] += 1
        if calls["count"] == 1:
            first_fetch_started.set()
            assert release_first_fetch.wait(timeout=5)
        environ["TEST_PROVIDER_API_KEY"] = "profile-only"
        return SimpleNamespace(
            sources=[SimpleNamespace()],
            provenance={
                "TEST_PROVIDER_API_KEY": SimpleNamespace(source="fake")
            },
        )

    monkeypatch.setattr(registry, "apply_all", _fake_apply_all)
    result: dict[str, str] = {}

    worker = threading.Thread(
        target=lambda: result.update(env_loader.hydrate_profile_secret_sources(home))
    )
    worker.start()
    assert first_fetch_started.wait(timeout=5)
    env_loader.reset_secret_source_cache()
    release_first_fetch.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert calls["count"] == 2
    assert result == {"TEST_PROVIDER_API_KEY": "profile-only"}
    assert env_loader.get_secret_source_values(home) == result


def test_startup_source_apply_shares_the_same_home_hydration_lock(
    tmp_path, monkeypatch
):
    """Concurrent startup calls for one home produce exactly one source fetch."""
    from types import SimpleNamespace

    from agent.secret_sources import registry

    home = tmp_path / "secondary"
    home.mkdir()
    monkeypatch.setattr(
        env_loader, "_load_secrets_config", lambda _home: {"fake": {"enabled": True}}
    )
    first_fetch_started = threading.Event()
    release_first_fetch = threading.Event()
    calls = {"count": 0}

    def _fake_apply_all(_cfg, _home):
        calls["count"] += 1
        first_fetch_started.set()
        assert release_first_fetch.wait(timeout=5)
        return SimpleNamespace(
            sources=[
                SimpleNamespace(applied=[], result=SimpleNamespace(error="", warnings=[]))
            ],
            applied_any=False,
            conflicts=[],
        )

    monkeypatch.setattr(registry, "apply_all", _fake_apply_all)
    workers = [
        threading.Thread(target=env_loader._apply_external_secret_sources, args=(home,))
        for _ in range(2)
    ]
    workers[0].start()
    assert first_fetch_started.wait(timeout=5)
    workers[1].start()
    release_first_fetch.set()
    for worker in workers:
        worker.join(timeout=5)

    assert all(not worker.is_alive() for worker in workers)
    assert calls["count"] == 1
