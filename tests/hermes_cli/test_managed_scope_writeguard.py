"""Write-guard tests — managed keys can't be set/removed by the user."""
import pytest


@pytest.fixture
def homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    import hermes_cli.config as cfg
    from hermes_cli import managed_scope

    cfg._LOAD_CONFIG_CACHE.clear()
    cfg._RAW_CONFIG_CACHE.clear()
    managed_scope.invalidate_managed_cache()
    (managed / "config.yaml").write_text(
        "model:\n  default: managed/model\n", encoding="utf-8"
    )
    managed_scope.invalidate_managed_cache()
    return home, managed


def test_config_set_managed_key_rejected(homes, capsys):
    from hermes_cli.config import set_config_value

    with pytest.raises(SystemExit) as exc:
        set_config_value("model.default", "user/override")
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "managed" in (captured.out + captured.err).lower()


def test_config_set_managed_key_does_not_write(homes):
    from hermes_cli.config import set_config_value, read_raw_config

    try:
        set_config_value("model.default", "user/override")
    except SystemExit:
        pass
    raw = read_raw_config()
    assert raw.get("model", {}).get("default") != "user/override"


def test_config_set_unmanaged_key_still_works(homes):
    from hermes_cli.config import set_config_value, read_raw_config

    set_config_value("model.fallback", "user/fb")  # not managed
    assert read_raw_config().get("model", {}).get("fallback") == "user/fb"


# ── env write guards ─────────────────────────────────────────────────────────


@pytest.fixture
def env_homes(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    managed = tmp_path / "managed"
    managed.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(managed))
    (managed / ".env").write_text(
        "OPENAI_API_BASE=https://org.example/v1\n", encoding="utf-8"
    )
    from hermes_cli import managed_scope

    managed_scope.invalidate_managed_cache()
    return home, managed


def test_save_env_value_managed_key_rejected(env_homes, capsys):
    from hermes_cli.config import save_env_value, get_env_path

    save_env_value("OPENAI_API_BASE", "https://user.example/v1")
    assert "managed" in capsys.readouterr().err.lower()
    env_path = get_env_path()
    body = env_path.read_text() if env_path.exists() else ""
    assert "user.example" not in body


def test_remove_env_value_managed_key_rejected(env_homes, capsys):
    from hermes_cli.config import remove_env_value

    result = remove_env_value("OPENAI_API_BASE")
    assert result is False
    assert "managed" in capsys.readouterr().err.lower()


def test_save_env_value_unmanaged_key_still_works(env_homes):
    from hermes_cli.config import save_env_value, get_env_value

    save_env_value("SOME_OTHER_VALUE", "abc123")
    assert get_env_value("SOME_OTHER_VALUE") == "abc123"


# ── bulk save strips managed leaves ──────────────────────────────────────────


def test_save_config_strips_managed_leaves(homes, capsys):
    from hermes_cli.config import save_config, read_raw_config

    # 'model.default' is managed (homes fixture); 'model.fallback' is not.
    save_config({"model": {"default": "user/override", "fallback": "user/fb"}})
    raw = read_raw_config()
    assert raw.get("model", {}).get("default") != "user/override"  # stripped
    assert raw.get("model", {}).get("fallback") == "user/fb"  # kept
    assert "managed" in capsys.readouterr().err.lower()


def test_save_model_choice_respects_full_managed_write_lock(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    original = "model:\n  default: original-model\n"
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED", "homebrew")

    from hermes_cli.auth import _save_model_choice

    _save_model_choice("forbidden-model")

    assert config_path.read_text(encoding="utf-8") == original
    assert "managed" in capsys.readouterr().err.lower()


def test_platform_raw_write_respects_full_managed_write_lock(
    tmp_path, monkeypatch, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    config_path = home / "config.yaml"
    original = "platforms:\n  email:\n    mode: original\n"
    config_path.write_text(original, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_MANAGED", "homebrew")

    from hermes_cli.config import write_platform_config_field

    write_platform_config_field("email", "mode", "forbidden", raw=True)

    assert config_path.read_text(encoding="utf-8") == original
    assert "managed" in capsys.readouterr().err.lower()


def test_save_model_choice_strips_managed_model_leaf(homes, capsys):
    home, _managed = homes
    (home / "config.yaml").write_text(
        "model:\n  default: old-user-model\n  fallback: user/fallback\n",
        encoding="utf-8",
    )
    from hermes_cli.auth import _save_model_choice
    from hermes_cli.config import read_raw_config

    _save_model_choice("forbidden-model")

    raw = read_raw_config()
    assert raw.get("model", {}).get("default") != "forbidden-model"
    assert raw["model"]["fallback"] == "user/fallback"
    assert "managed" in capsys.readouterr().err.lower()


def test_platform_raw_write_strips_managed_platform_leaf(homes, capsys):
    home, managed = homes
    (managed / "config.yaml").write_text(
        "platforms:\n  email:\n    mode: admin-only\n",
        encoding="utf-8",
    )
    (home / "config.yaml").write_text(
        "platforms:\n  email:\n    mode: old-user\n    sibling: keep\n",
        encoding="utf-8",
    )
    from hermes_cli import managed_scope
    from hermes_cli.config import read_raw_config, write_platform_config_field

    managed_scope.invalidate_managed_cache()
    write_platform_config_field("email", "mode", "forbidden", raw=True)

    raw = read_raw_config()
    assert raw.get("platforms", {}).get("email", {}).get("mode") != "forbidden"
    assert raw["platforms"]["email"]["sibling"] == "keep"
    assert "managed" in capsys.readouterr().err.lower()
