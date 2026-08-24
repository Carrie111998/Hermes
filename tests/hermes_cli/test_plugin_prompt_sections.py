from __future__ import annotations

import logging
import os
from pathlib import Path
from types import MappingProxyType

import pytest

from hermes_cli.plugins import (
    MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS,
    PluginContext,
    PluginManager,
    PluginManifest,
)


def _context(manager: PluginManager, name: str = "example-plugin") -> PluginContext:
    return PluginContext(
        PluginManifest(name=name, key=name, source="user"),
        manager,
    )


def _write_section_limits_config(**limits: int) -> None:
    """Write plugins.* section limits into this test's isolated HERMES_HOME."""
    home = Path(os.environ["HERMES_HOME"])
    lines = ["plugins:"]
    lines += [f"  {key}: {value}" for key, value in limits.items()]
    (home / "config.yaml").write_text("\n".join(lines) + "\n")


def test_registration_validates_stable_id_position_budget_and_duplicates():
    manager = PluginManager()
    ctx = _context(manager)

    for invalid_id in ("", "UPPER.case", "has space", "line\nbreak", "x" * 129):
        with pytest.raises(ValueError):
            ctx.register_system_prompt_section(invalid_id, "content")

    with pytest.raises(ValueError):
        ctx.register_system_prompt_section("example.rules", "content", position="priority-17")
    with pytest.raises(ValueError):
        ctx.register_system_prompt_section("example.rules", "content", max_chars=0)

    ctx.register_system_prompt_section("example.rules", "content")
    with pytest.raises(ValueError, match="already registered"):
        _context(manager, "other-plugin").register_system_prompt_section(
            "example.rules", "other content"
        )


def test_render_is_deterministic_bounded_and_session_info_is_read_only(caplog):
    manager = PluginManager()
    ctx = _context(manager)
    observed = []

    def render_b(info):
        observed.append(info)
        with pytest.raises(TypeError):
            info["session_id"] = "changed"
        return "B"

    ctx.register_system_prompt_section("example.z", render_b, max_chars=4)
    ctx.register_system_prompt_section("example.a", "A", max_chars=4)
    ctx.register_system_prompt_section("example.too-large", "12345", max_chars=4)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        rendered = manager.render_system_prompt_sections({"session_id": "session-1"})

    assert [(item.id, item.content) for item in rendered] == [
        ("example.a", "A"),
        ("example.z", "B"),
    ]
    assert isinstance(observed[0], MappingProxyType)
    assert observed[0]["session_id"] == "session-1"
    assert "exceeded max_chars" in caplog.text


def test_render_fails_open_for_callback_failure_wrong_type_and_aggregate_budget(caplog):
    manager = PluginManager()
    ctx = _context(manager)

    def boom(_info):
        raise RuntimeError("plugin exploded")

    ctx.register_system_prompt_section("example.boom", boom)
    ctx.register_system_prompt_section("example.wrong", lambda _info: {"not": "text"})
    chunk = (MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS // 2) - 200
    ctx.register_system_prompt_section("example.first", "a" * chunk, max_chars=chunk)
    ctx.register_system_prompt_section("example.second", "b" * chunk, max_chars=chunk)
    ctx.register_system_prompt_section("example.zzlast", "c" * 500, max_chars=500)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        rendered = manager.render_system_prompt_sections({})

    assert [item.id for item in rendered] == ["example.first", "example.second"]
    assert "plugin exploded" in caplog.text
    assert "returned dict, not str" in caplog.text
    assert "aggregate" in caplog.text


def test_configured_limits_allow_large_sections_and_aggregate(caplog):
    """#92774: config.yaml plugins.* knobs raise the hard-coded caps."""
    _write_section_limits_config(
        system_prompt_section_max_chars=65536,
        system_prompt_section_total_chars=131072,
    )
    manager = PluginManager()
    ctx = _context(manager)

    big = "R" * 43_000  # the reporter's pinned-files scale
    ctx.register_system_prompt_section("example.pinned", big, max_chars=len(big))
    chunk = "x" * 3_900
    for i in range(3):
        ctx.register_system_prompt_section(f"example.ref-{i}", chunk)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        rendered = manager.render_system_prompt_sections({})

    assert [item.id for item in rendered] == [
        "example.pinned",
        "example.ref-0",
        "example.ref-1",
        "example.ref-2",
    ]
    assert "skipped" not in caplog.text
    assert manager.dropped_prompt_section_reports() == []


def test_configured_max_sections_and_default_max_chars_follow_config():
    _write_section_limits_config(
        system_prompt_section_max_chars=10_000,
        system_prompt_section_total_chars=131072,
        system_prompt_section_max_sections=2,
    )
    manager = PluginManager()
    ctx = _context(manager)

    # Default max_chars stays at the built-in 4000 even when the ceiling is
    # raised — larger sections must opt in explicitly.
    ctx.register_system_prompt_section("example.a", "a" * 4_500, max_chars=5_000)
    with pytest.raises(ValueError, match="between 1 and 10000"):
        ctx.register_system_prompt_section("example.too-big", "x", max_chars=10_001)

    ctx.register_system_prompt_section("example.b", "b")
    ctx.register_system_prompt_section("example.c", "c")

    rendered = manager.render_system_prompt_sections({})
    assert [item.id for item in rendered] == ["example.a", "example.b"]
    reports = manager.dropped_prompt_section_reports()
    assert [r["id"] for r in reports] == ["example.c"]
    assert "section-count budget" in reports[0]["reason"]


def test_invalid_config_values_fall_back_to_defaults():
    home = Path(os.environ["HERMES_HOME"])
    (home / "config.yaml").write_text(
        "plugins:\n"
        "  system_prompt_section_max_chars: not-a-number\n"
        "  system_prompt_section_total_chars: -5\n"
        "  system_prompt_section_max_sections: 0\n"
    )
    manager = PluginManager()
    ctx = _context(manager)

    with pytest.raises(ValueError, match="between 1 and 4000"):
        ctx.register_system_prompt_section("example.big", "x", max_chars=4_001)

    chunk = (MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS // 2) - 200
    ctx.register_system_prompt_section("example.first", "a" * chunk, max_chars=chunk)
    ctx.register_system_prompt_section("example.second", "b" * chunk, max_chars=chunk)
    ctx.register_system_prompt_section("example.zzlast", "c" * 500, max_chars=500)

    rendered = manager.render_system_prompt_sections({})
    # Default 8000 aggregate + default 32 sections still enforced.
    assert [item.id for item in rendered] == ["example.first", "example.second"]


def test_dropped_sections_are_reported_not_just_logged(caplog):
    """#92774 ask 3: aggregate overflow must be visible beyond a log line."""
    manager = PluginManager()
    ctx = _context(manager)
    chunk = (MAX_SYSTEM_PROMPT_SECTIONS_TOTAL_CHARS // 2) - 200
    ctx.register_system_prompt_section("example.first", "a" * chunk, max_chars=chunk)
    ctx.register_system_prompt_section("example.second", "b" * chunk, max_chars=chunk)
    ctx.register_system_prompt_section("example.zzlast", "c" * 500, max_chars=500)

    with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
        manager.render_system_prompt_sections({})

    reports = manager.dropped_prompt_section_reports()
    assert [r["id"] for r in reports] == ["example.zzlast"]
    assert reports[0]["plugin"] == "example-plugin"
    assert "system_prompt_section_total_chars" in reports[0]["reason"]

    # And the drop count reaches the plugin-facing introspection surface.
    from hermes_cli.plugins import LoadedPlugin

    manager._plugins["example-plugin"] = LoadedPlugin(
        manifest=PluginManifest(name="example-plugin", key="example-plugin", source="user"),
        enabled=True,
    )
    info = manager.list_plugins()[0]
    assert info["prompt_sections_dropped"] == 1

    # A clean re-render resets the report.
    manager._system_prompt_sections.pop("example.zzlast")
    manager.render_system_prompt_sections({})
    assert manager.dropped_prompt_section_reports() == []
