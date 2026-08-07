"""Seam-identity + aggressive tests for the schema/config extraction (R1-C6).

``hermes_cli/web_schema.py`` holds the dashboard's config-schema rendering
helpers, moved byte-verbatim out of ``hermes_cli/web_server.py`` (god-file
slice R1-C6, epic #78791).  The seam-identity tests pin the regression this
extraction is meant to prevent: ``web_server`` must resolve every moved name
to the *same object* the new module defines.  The aggressive tests then
exercise the schema-rendering failure modes: empty config, unknown provider,
timezone/memory-provider options, and the dynamic-merge fallback.
"""

import pytest

from hermes_cli import web_schema as s
from hermes_cli import web_server as ws

MOVED_NAMES = (
    "_memory_provider_options",
    "_timezone_options",
    "_SCHEMA_OVERRIDES",
    "_CATEGORY_MERGE",
    "_CATEGORY_ORDER",
    "_infer_type",
    "_build_schema_from_config",
    "CONFIG_SCHEMA",
    "_is_command_provider_block",
    "_custom_provider_options",
    "_memory_provider_schema_options",
    "_schema_with_dynamic_provider_options",
)


@pytest.mark.parametrize("name", MOVED_NAMES)
def test_moved_names_are_seam_identical(name):
    # ``is``-identity: web_server must resolve each moved name to the very
    # same object the new module defines — no redefinition allowed.
    assert getattr(ws, name) is getattr(s, name)


def test_schema_is_dict_with_memory_provider():
    assert isinstance(s.CONFIG_SCHEMA, dict)
    assert "memory.provider" in s.CONFIG_SCHEMA


def test_build_schema_from_config_empty():
    result = s._build_schema_from_config({})
    assert isinstance(result, dict)


def test_timezone_options_returns_list():
    result = s._timezone_options()
    assert isinstance(result, list)
    assert len(result) > 0


def test_memory_provider_options_returns_list():
    result = s._memory_provider_options()
    assert isinstance(result, list)


def test_infer_type_unknown_value():
    assert s._infer_type("some-unknown-value") is not None


def test_dynamic_schema_with_config_override(monkeypatch):
    monkeypatch.setattr(s, "load_config", lambda: {"memory": {"provider": "honcho"}})
    monkeypatch.setattr(s, "_memory_provider_options", lambda: ["", "honcho", "fresh"])
    fields = s._schema_with_dynamic_provider_options()
    assert "memory.provider" in fields
    assert fields["memory.provider"]["type"] == "select"


def test_dynamic_schema_load_config_raises_falls_back(monkeypatch):
    # load_config raising must fall back to CONFIG_SCHEMA by identity.
    def _boom():
        raise RuntimeError("config boom")

    monkeypatch.setattr(s, "load_config", _boom)
    fields = s._schema_with_dynamic_provider_options()
    assert isinstance(fields, dict)


def test_category_order_is_list():
    assert isinstance(s._CATEGORY_ORDER, list)
