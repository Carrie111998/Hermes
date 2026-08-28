"""Validation for web provider values written through ``hermes config``."""

import os
from unittest.mock import patch

import pytest
import yaml

from hermes_cli.config import set_config_value


@pytest.fixture(autouse=True)
def _isolated_hermes_home(tmp_path):
    """Keep config writes and plugin registry scope away from the real profile."""
    with patch.dict(os.environ, {"HERMES_HOME": str(tmp_path)}):
        yield tmp_path


def test_set_rejects_unknown_search_backend_without_persisting(
    _isolated_hermes_home, capsys
):
    with pytest.raises(SystemExit) as exc_info:
        set_config_value("web.search_backend", "smart-web")

    assert exc_info.value.code == 1
    assert not (_isolated_hermes_home / "config.yaml").exists()
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "smart-web" in combined
    assert "web.search_backend" in combined
    assert "--force" in combined


def test_set_rejects_unknown_extract_backend_without_persisting(
    _isolated_hermes_home, capsys
):
    with pytest.raises(SystemExit) as exc_info:
        set_config_value("web.extract_backend", "smart-web")

    assert exc_info.value.code == 1
    assert not (_isolated_hermes_home / "config.yaml").exists()
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "smart-web" in combined
    assert "web.extract_backend" in combined
    assert "--force" in combined


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("web.search_backend", "searxng"),
        ("web.extract_backend", "keenable"),
    ],
)
def test_set_accepts_known_provider_without_credentials(
    _isolated_hermes_home, key, value
):
    set_config_value(key, value)

    config = yaml.safe_load(
        (_isolated_hermes_home / "config.yaml").read_text(encoding="utf-8")
    )
    section, leaf = key.split(".")
    assert config[section][leaf] == value


def test_set_rejects_provider_without_requested_capability(
    _isolated_hermes_home, capsys
):
    with pytest.raises(SystemExit) as exc_info:
        set_config_value("web.extract_backend", "searxng")

    assert exc_info.value.code == 1
    assert not (_isolated_hermes_home / "config.yaml").exists()
    assert "does not support extraction" in capsys.readouterr().err


def test_set_force_allows_unregistered_plugin_provider(_isolated_hermes_home):
    set_config_value("web.search_backend", "my-private-search", force=True)

    config = yaml.safe_load(
        (_isolated_hermes_home / "config.yaml").read_text(encoding="utf-8")
    )
    assert config["web"]["search_backend"] == "my-private-search"


@pytest.mark.parametrize("key", ["search_backend", "extract_backend"])
def test_config_check_fails_for_stored_unknown_web_backend(
    _isolated_hermes_home, capsys, key
):
    from argparse import Namespace

    from hermes_cli.config import config_command

    (_isolated_hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"web": {key: "smart-web"}}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        config_command(Namespace(config_command="check"))

    assert exc_info.value.code == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert f"web.{key}" in combined
    assert "smart-web" in combined
    assert "hermes config set" in combined
    assert "--force" in combined
