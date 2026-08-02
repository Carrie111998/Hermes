from agent.auxiliary_client import _build_call_kwargs
from agent.chat_completion_helpers import _enforce_summary_openrouter_zdr
from agent.openrouter_zdr import enforce_openrouter_zdr


def test_config_set_enables_final_boundary_enforcement(monkeypatch, tmp_path):
    import hermes_cli.config as config_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()

    config_mod.set_config_value("openrouter.zdr", "true")
    kwargs = {"extra_body": {"provider": {"zdr": False}}}
    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )

    assert config_mod.load_config()["openrouter"]["zdr"] is True
    assert kwargs["extra_body"]["provider"]["zdr"] is True


def _write_raw_config(monkeypatch, tmp_path, text):
    import hermes_cli.config as config_mod

    home = tmp_path / ".hermes"
    home.mkdir()
    (home / "config.yaml").write_text(text, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    config_mod._LOAD_CONFIG_CACHE.clear()
    config_mod._RAW_CONFIG_CACHE.clear()
    config_mod._LAST_EXPANDED_CONFIG_BY_PATH.clear()
    getattr(config_mod, "_CONFIG_LOAD_FAILURE_BY_PATH").clear()
    return config_mod


def test_invalid_yaml_enforces_zdr_fail_closed(monkeypatch, tmp_path, caplog):
    _write_raw_config(monkeypatch, tmp_path, "openrouter: [")
    kwargs = {"extra_body": {"provider": {"zdr": False}}}

    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )

    assert kwargs["extra_body"]["provider"]["zdr"] is True
    assert "enforcing ZDR fail closed" in caplog.text


def test_parse_failure_state_clears_after_config_is_fixed(monkeypatch, tmp_path):
    config_mod = _write_raw_config(monkeypatch, tmp_path, "openrouter: [")
    failed_kwargs = {"extra_body": {"provider": {"zdr": False}}}
    enforce_openrouter_zdr(
        failed_kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )
    assert failed_kwargs["extra_body"]["provider"]["zdr"] is True

    config_mod.get_config_path().write_text(
        "openrouter:\n  zdr: false\n", encoding="utf-8"
    )
    fixed_kwargs = {"extra_body": {"provider": {"zdr": False}}}
    enforce_openrouter_zdr(
        fixed_kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )

    assert fixed_kwargs["extra_body"]["provider"]["zdr"] is False


def test_quoted_zdr_value_enforces_zdr_fail_closed(monkeypatch, tmp_path, caplog):
    _write_raw_config(monkeypatch, tmp_path, 'openrouter:\n  zdr: "true"\n')
    kwargs = {"extra_body": {"provider": {"zdr": False}}}

    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )

    assert kwargs["extra_body"]["provider"]["zdr"] is True
    assert "openrouter.zdr must be a boolean" in caplog.text


def test_non_mapping_config_root_enforces_zdr_fail_closed(
    monkeypatch, tmp_path, caplog
):
    _write_raw_config(monkeypatch, tmp_path, "- openrouter\n- zdr\n")
    kwargs = {"extra_body": {"provider": {"zdr": False}}}

    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )

    assert kwargs["extra_body"]["provider"]["zdr"] is True
    assert "could not be parsed" in caplog.text


def test_explicit_false_from_real_config_preserves_caller_policy(
    monkeypatch, tmp_path
):
    _write_raw_config(monkeypatch, tmp_path, "openrouter:\n  zdr: false\n")
    kwargs = {"extra_body": {"provider": {"zdr": False}}}

    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )

    assert kwargs["extra_body"]["provider"]["zdr"] is False


def test_shared_enforcement_overrides_false(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.openrouter_zdr_enabled", lambda: True)
    kwargs = {
        "extra_body": {
            "provider": {
                "sort": "price",
                "data_collection": "allow",
                "zdr": False,
            }
        }
    }
    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )
    assert kwargs["extra_body"]["provider"] == {
        "sort": "price",
        "data_collection": "allow",
        "zdr": True,
    }


def test_shared_enforcement_fails_closed_when_config_read_raises(monkeypatch, caplog):
    def raise_config_error():
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(
        "hermes_cli.config.openrouter_zdr_enabled", raise_config_error
    )
    kwargs = {"extra_body": {"provider": {"zdr": False}}}

    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )

    assert kwargs["extra_body"]["provider"]["zdr"] is True
    assert "enforcing ZDR fail closed" in caplog.text


def test_shared_enforcement_skips_native_gemini(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.openrouter_zdr_enabled", lambda: True)
    kwargs = {}
    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://generativelanguage.googleapis.com/v1beta",
    )
    assert kwargs == {}


def test_auxiliary_openrouter_enforces_zdr_after_caller_body(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.openrouter_zdr_enabled", lambda: True)
    kwargs = _build_call_kwargs(
        "openrouter",
        "anthropic/claude-sonnet-4.6",
        [{"role": "user", "content": "ping"}],
        extra_body={"provider": {"zdr": False, "sort": "price"}},
        base_url="https://openrouter.ai/api/v1",
    )
    assert kwargs["extra_body"]["provider"] == {"zdr": True, "sort": "price"}


def test_auxiliary_fallback_label_uses_openrouter_base_url(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.openrouter_zdr_enabled", lambda: True)
    kwargs = _build_call_kwargs(
        "fallback_chain[0](openrouter)",
        "anthropic/claude-sonnet-4.6",
        [{"role": "user", "content": "ping"}],
        base_url="https://openrouter.ai/api/v1",
    )
    assert kwargs["extra_body"]["provider"] == {"zdr": True}


def test_summary_direct_call_enforces_zdr(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.openrouter_zdr_enabled", lambda: True)
    agent = type(
        "Agent",
        (),
        {
            "provider": "openrouter",
            "base_url": "https://openrouter.ai/api/v1",
            "_is_openrouter_url": lambda self: True,
        },
    )()
    kwargs = {"extra_body": {"provider": {"zdr": False}}}
    _enforce_summary_openrouter_zdr(agent, kwargs)
    assert kwargs["extra_body"]["provider"]["zdr"] is True


def test_summary_detection_failure_uses_strict_host_fallback(monkeypatch, caplog):
    monkeypatch.setattr("hermes_cli.config.openrouter_zdr_enabled", lambda: True)

    def raise_detection_error(self):
        raise RuntimeError("detection unavailable")

    agent = type(
        "Agent",
        (),
        {
            "provider": "fallback",
            "base_url": "https://openrouter.ai/api/v1",
            "_is_openrouter_url": raise_detection_error,
        },
    )()
    kwargs = {"extra_body": {"provider": {"zdr": False}}}

    _enforce_summary_openrouter_zdr(agent, kwargs)

    assert kwargs["extra_body"]["provider"]["zdr"] is True
    assert "host fallback selected is_openrouter=True" in caplog.text


def test_summary_detection_failure_does_not_modify_other_hosts(monkeypatch, caplog):
    monkeypatch.setattr("hermes_cli.config.openrouter_zdr_enabled", lambda: True)

    def raise_detection_error(self):
        raise RuntimeError("detection unavailable")

    agent = type(
        "Agent",
        (),
        {
            "provider": "custom",
            "base_url": "https://example.com/v1",
            "_is_openrouter_url": raise_detection_error,
        },
    )()
    kwargs = {}

    _enforce_summary_openrouter_zdr(agent, kwargs)

    assert kwargs == {}
    assert "host fallback selected is_openrouter=False" in caplog.text


def test_disabled_zdr_preserves_caller_policy(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.openrouter_zdr_enabled", lambda: False)
    kwargs = {"extra_body": {"provider": {"zdr": False}}}
    enforce_openrouter_zdr(
        kwargs,
        is_openrouter=True,
        base_url="https://openrouter.ai/api/v1",
    )
    assert kwargs["extra_body"]["provider"]["zdr"] is False