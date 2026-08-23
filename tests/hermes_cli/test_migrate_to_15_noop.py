"""_migrate_to_15 must be a faithful no-op (#93533).

The v14→v15 step historically wrote ``display.interim_assistant_messages:
true`` for installs lacking the key. That value equals the schema default,
so ``_persist_migration``'s strip-defaults invariant removed it again
immediately — while the migration printed "✓ Added …" and recorded a
``config_added`` entry that never landed on disk.
"""

from pathlib import Path

from hermes_cli import config as c
from hermes_cli import config_migrations as m


def _results():
    return {
        "config_added": [],
        "config_removed": [],
        "config_changed": [],
        "errors": [],
    }


def _write_config(text: str) -> Path:
    path = c.get_hermes_config_path() if hasattr(c, "get_hermes_config_path") else (
        c.get_hermes_home() / "config.yaml"
    )
    path.write_text(text, encoding="utf-8")
    return path


def test_v15_migration_persists_nothing_and_reports_nothing():
    _write_config("_config_version: 14\ndisplay:\n  skin: mono\n")
    before = c.read_raw_config()

    results = _results()
    m._migrate_to_15(results, quiet=True)

    after = c.read_raw_config()
    assert after == before, "migration must not touch a default-equal value"
    assert results["config_added"] == [], (
        "must not report a write that never landed on disk"
    )
    assert "interim_assistant_messages" not in after.get("display", {})


def test_effective_value_still_resolves_true_via_defaults():
    _write_config("_config_version: 14\ndisplay:\n  skin: mono\n")
    m._migrate_to_15(_results(), quiet=True)

    cfg = c.load_config_readonly()
    assert cfg["display"]["interim_assistant_messages"] is True


def test_explicit_user_value_is_left_untouched():
    _write_config(
        "_config_version: 14\ndisplay:\n"
        "  interim_assistant_messages: false\n"
    )
    results = _results()
    m._migrate_to_15(results, quiet=True)

    raw = c.read_raw_config()
    assert raw["display"]["interim_assistant_messages"] is False
    assert results["config_added"] == []
