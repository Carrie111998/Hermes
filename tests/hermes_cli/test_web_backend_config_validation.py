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


def test_set_accepts_managed_nous_alias_as_firecrawl_backend(
    _isolated_hermes_home
):
    set_config_value("web.backend", "nous")

    config = yaml.safe_load(
        (_isolated_hermes_home / "config.yaml").read_text(encoding="utf-8")
    )
    # Preserve the managed-routing selection; only validation normalizes it to
    # the search+extract-capable Firecrawl provider implementation.
    assert config["web"]["backend"] == "nous"


def test_set_rejects_provider_without_requested_capability(
    _isolated_hermes_home, capsys
):
    with pytest.raises(SystemExit) as exc_info:
        set_config_value("web.extract_backend", "searxng")

    assert exc_info.value.code == 1
    assert not (_isolated_hermes_home / "config.yaml").exists()
    assert "does not support extraction" in capsys.readouterr().err


def test_set_rejects_search_only_provider_for_shared_backend(
    _isolated_hermes_home, capsys
):
    with pytest.raises(SystemExit) as exc_info:
        set_config_value("web.backend", "searxng")

    assert exc_info.value.code == 1
    assert not (_isolated_hermes_home / "config.yaml").exists()
    error = capsys.readouterr().err
    assert "web.backend" in error
    assert "searxng" in error
    assert "does not support extraction" in error


def test_set_rejects_backend_whose_plugin_is_disabled(
    _isolated_hermes_home, capsys
):
    config_path = _isolated_hermes_home / "config.yaml"
    config_path.write_text(
        yaml.safe_dump({"plugins": {"disabled": ["web/firecrawl"]}}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        set_config_value("web.search_backend", "firecrawl")

    assert exc_info.value.code == 1
    assert yaml.safe_load(config_path.read_text(encoding="utf-8")) == {
        "plugins": {"disabled": ["web/firecrawl"]}
    }
    error = capsys.readouterr().err
    assert "firecrawl" in error
    assert "web/firecrawl" in error
    assert "disabled" in error


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


def test_config_check_reports_backend_whose_plugin_is_disabled(
    _isolated_hermes_home, capsys
):
    from argparse import Namespace

    from hermes_cli.config import config_command

    (_isolated_hermes_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "plugins": {"disabled": ["web/firecrawl"]},
                "web": {"extract_backend": "firecrawl"},
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        config_command(Namespace(config_command="check"))

    assert exc_info.value.code == 1
    combined = "".join(capsys.readouterr())
    assert "web.extract_backend" in combined
    assert "web/firecrawl" in combined
    assert "disabled" in combined


def test_config_check_reports_search_only_shared_backend(
    _isolated_hermes_home, capsys
):
    from argparse import Namespace

    from hermes_cli.config import config_command

    (_isolated_hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"web": {"backend": "searxng"}}),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc_info:
        config_command(Namespace(config_command="check"))

    assert exc_info.value.code == 1
    combined = "".join(capsys.readouterr())
    assert "web.backend" in combined
    assert "searxng" in combined
    assert "does not support extraction" in combined


def test_config_check_accepts_managed_nous_alias(_isolated_hermes_home, capsys):
    from argparse import Namespace

    from hermes_cli.config import config_command

    (_isolated_hermes_home / "config.yaml").write_text(
        yaml.safe_dump({"web": {"backend": "nous"}}),
        encoding="utf-8",
    )

    config_command(Namespace(config_command="check"))

    combined = "".join(capsys.readouterr())
    assert "unknown web provider 'nous'" not in combined
    assert "Config validation" not in combined
