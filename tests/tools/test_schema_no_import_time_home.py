"""Regression tests for import-time display_hermes_home() in tool schemas (#95685).

Module-level schema f-strings evaluated ``display_hermes_home()`` once at
import, freezing whichever profile first imported the module into the tool
description for the whole process lifetime — every later session in any
other profile saw the wrong profile's path. The schemas are now
profile-neutral strings; these pins keep any future schema edit from
reintroducing a per-profile path (or the import) into these static
descriptions.
"""

import re

import tools.cronjob_tools as cronjob_tools
import tools.skill_manager_tool as skill_manager_tool
import tools.tts_tool as tts_tool


def _schema_text(schema) -> str:
    parts = [str(schema.get("description") or "")]
    for prop in (schema.get("parameters", {}).get("properties") or {}).values():
        parts.append(str(prop.get("description") or ""))
    return "\n".join(parts)


def test_cronjob_schema_is_profile_neutral():
    text = _schema_text(cronjob_tools.CRONJOB_SCHEMA)
    assert "profiles/" not in text
    assert "scripts/ directory in the Hermes home" in text


def test_skill_manage_schema_is_profile_neutral():
    text = _schema_text(skill_manager_tool.SKILL_MANAGE_SCHEMA)
    assert "profiles/" not in text
    assert "skills/ directory in the Hermes home" in text


def test_tts_schema_is_profile_neutral():
    text = _schema_text(tts_tool.TTS_SCHEMA if hasattr(tts_tool, "TTS_SCHEMA") else tts_tool.SCHEMA)
    assert "profiles/" not in text
    assert "audio_cache/" in text


def test_no_import_time_home_call_remains_in_schema_modules():
    """The static schemas must not call profile-dependent helpers at import.

    Scans the module source for f-string schema descriptions embedding
    display_hermes_home — the exact regression shape from #95685.
    """
    for mod in (cronjob_tools, skill_manager_tool, tts_tool):
        source = open(mod.__file__, encoding="utf-8").read()
        fstring_uses = re.findall(r"f\"[^\"]*display_hermes_home\(\)", source)
        assert not fstring_uses, (mod.__name__, fstring_uses)
