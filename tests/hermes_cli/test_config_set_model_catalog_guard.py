"""Regression tests for the write-time model-slug catalog guard (#97656).

`hermes config set` accepted any string for model-routing keys; a one-character
typo was written silently and only surfaced as HTTP 400s at subagent dispatch
time. The guard warns (and confirms on an interactive TTY) when the value is
absent from the resolved provider's cached catalog — fail-open everywhere else.

These tests pin:
- the warning fires only with a non-empty catalog and an absent slug;
- fail-open on empty catalogs, fetch errors, and unknown providers;
- direct endpoints (base_url) and the auxiliary "auto" chain are never checked
  (their runtime provider is not statically predictable);
- provider-prefixed values are checked in stripped form too;
- --force and non-TTY runs never block; a declined TTY confirmation aborts.
"""

import pytest
import yaml

from hermes_cli import config as cfg


class _FakeTTY:
    def isatty(self):
        return True

    def write(self, s):
        return len(s)

    def flush(self):
        pass


def _seed_config(tmp_path, config: dict) -> None:
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config))


def _read(tmp_path, *path):
    data = yaml.safe_load((tmp_path / "config.yaml").read_text()) or {}
    node = data
    for seg in path:
        node = node[seg]
    return node


def _mock_catalog(monkeypatch, ids, calls=None):
    def fake(provider, **kwargs):
        if calls is not None:
            calls.append(provider)
        return list(ids)

    monkeypatch.setattr("hermes_cli.models.cached_provider_model_ids", fake)


_CATALOG = ["upstage/solar-pro4", "deepseek/deepseek-v4-flash", "zai/glm-5.3"]


class TestWarningFires:
    def test_typo_slug_warns_but_writes_non_tty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, _CATALOG)

        cfg.set_config_value("delegation.model", "upstage/solar-pro-4")

        assert "not in provider" in capsys.readouterr().err
        assert _read(tmp_path, "delegation", "model") == "upstage/solar-pro-4"

    def test_model_default_checked_against_global_provider(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, _CATALOG)

        cfg.set_config_value("model.default", "totally-fake-model-xyz")

        assert "not in provider" in capsys.readouterr().err
        assert _read(tmp_path, "model", "default") == "totally-fake-model-xyz"

    def test_provider_prefixed_value_checked_stripped(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, _CATALOG)

        cfg.set_config_value("delegation.model", "openrouter/upstage/solar-pro4")

        assert "not in provider" not in capsys.readouterr().err
        assert _read(tmp_path, "delegation", "model") == "openrouter/upstage/solar-pro4"

    def test_valid_slug_no_warning(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, _CATALOG)

        cfg.set_config_value("delegation.model", "upstage/solar-pro4")

        assert "not in provider" not in capsys.readouterr().err


class TestFailOpen:
    def test_empty_catalog_never_warns(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, [])

        cfg.set_config_value("delegation.model", "upstage/solar-pro-4")

        assert "not in provider" not in capsys.readouterr().err
        assert _read(tmp_path, "delegation", "model") == "upstage/solar-pro-4"

    def test_catalog_exception_never_blocks(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )

        def boom(provider, **kwargs):
            raise RuntimeError("network down")

        monkeypatch.setattr("hermes_cli.models.cached_provider_model_ids", boom)

        cfg.set_config_value("delegation.model", "upstage/solar-pro-4")

        assert _read(tmp_path, "delegation", "model") == "upstage/solar-pro-4"

    def test_unknown_provider_skipped(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path,
            {"model": {"default": "x", "provider": "my-own-endpoint-thing"}},
        )
        calls = []
        _mock_catalog(monkeypatch, _CATALOG, calls)

        cfg.set_config_value("delegation.model", "whatever")

        assert calls == []
        assert _read(tmp_path, "delegation", "model") == "whatever"

    def test_non_model_key_never_touched(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        calls = []
        _mock_catalog(monkeypatch, _CATALOG, calls)

        cfg.set_config_value("agent.max_turns", "10")

        assert calls == []


class TestScopeBoundaries:
    def test_delegation_base_url_direct_endpoint_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path,
            {
                "model": {"default": "zai/glm-5.3", "provider": "openrouter"},
                "delegation": {
                    "base_url": "https://litellm.internal/v1",
                    "provider": "",
                },
            },
        )
        calls = []
        _mock_catalog(monkeypatch, _CATALOG, calls)

        cfg.set_config_value("delegation.model", "internal-model")

        assert calls == []
        assert _read(tmp_path, "delegation", "model") == "internal-model"

    def test_auxiliary_auto_provider_skipped(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path,
            {
                "model": {"default": "zai/glm-5.3", "provider": "openrouter"},
                "auxiliary": {"goal_judge": {"provider": "auto"}},
            },
        )
        calls = []
        _mock_catalog(monkeypatch, _CATALOG, calls)

        cfg.set_config_value("auxiliary.goal_judge.model", "some-auto-chain-model")

        assert calls == []
        assert (
            _read(tmp_path, "auxiliary", "goal_judge", "model")
            == "some-auto-chain-model"
        )

    def test_auxiliary_explicit_provider_checked(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path,
            {
                "model": {"default": "zai/glm-5.3", "provider": "openrouter"},
                "auxiliary": {"goal_judge": {"provider": "zai"}},
            },
        )
        calls = []
        _mock_catalog(monkeypatch, ["glm-5.3", "glm-5.3-air"], calls)

        cfg.set_config_value("auxiliary.goal_judge.model", "glm-5.3")

        assert calls == ["zai"]
        assert "not in provider" not in capsys.readouterr().err

    def test_delegation_provider_pinned_overrides_global(
        self, tmp_path, monkeypatch, capsys
    ):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path,
            {
                "model": {"default": "zai/glm-5.3", "provider": "openrouter"},
                "delegation": {"provider": "zai"},
            },
        )
        calls = []
        _mock_catalog(monkeypatch, ["glm-5.3"], calls)

        cfg.set_config_value("delegation.model", "glm-5.3")

        assert calls == ["zai"]
        assert "not in provider" not in capsys.readouterr().err


class TestConfirmationFlow:
    def test_tty_decline_aborts_write(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, _CATALOG)
        monkeypatch.setattr("sys.stdin", _FakeTTY())
        monkeypatch.setattr("sys.stdout", _FakeTTY())
        monkeypatch.setattr("builtins.input", lambda prompt="": "n")

        with pytest.raises(SystemExit) as exc:
            cfg.set_config_value("delegation.model", "upstage/solar-pro-4")

        assert exc.value.code == 1
        data = yaml.safe_load((tmp_path / "config.yaml").read_text()) or {}
        assert "delegation" not in data

    def test_tty_accept_writes(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, _CATALOG)
        monkeypatch.setattr("sys.stdin", _FakeTTY())
        monkeypatch.setattr("sys.stdout", _FakeTTY())
        monkeypatch.setattr("builtins.input", lambda prompt="": "y")

        cfg.set_config_value("delegation.model", "upstage/solar-pro-4")

        assert _read(tmp_path, "delegation", "model") == "upstage/solar-pro-4"

    def test_force_skips_confirmation_on_tty(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, _CATALOG)
        monkeypatch.setattr("sys.stdin", _FakeTTY())
        monkeypatch.setattr("sys.stdout", _FakeTTY())

        def no_input(prompt=""):
            raise AssertionError("input() must not be called with --force")

        monkeypatch.setattr("builtins.input", no_input)

        cfg.set_config_value("delegation.model", "upstage/solar-pro-4", force=True)

        assert _read(tmp_path, "delegation", "model") == "upstage/solar-pro-4"

    def test_tty_eof_declines(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        _seed_config(
            tmp_path, {"model": {"default": "zai/glm-5.3", "provider": "openrouter"}}
        )
        _mock_catalog(monkeypatch, _CATALOG)
        monkeypatch.setattr("sys.stdin", _FakeTTY())
        monkeypatch.setattr("sys.stdout", _FakeTTY())

        def eof_input(prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof_input)

        with pytest.raises(SystemExit):
            cfg.set_config_value("delegation.model", "upstage/solar-pro-4")
