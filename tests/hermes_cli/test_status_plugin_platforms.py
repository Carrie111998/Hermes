"""Regression tests for the plugin-platform list in ``hermes status`` (#96190).

``check_fn`` is the registry's PASSIVE dependency probe; for several plugins
(teams, photon, a2a) it answers "are the SDKs importable right now?", not "are
credentials configured". Reporting it as "configured" showed platforms with
only commented-out .env template lines as configured the moment the optional
SDKs landed in the venv. The status listing now follows the same semantic
chain the gateway uses for platform usability — ``is_connected`` ->
``validate_config`` — falling back to ``check_fn`` only for plugins that
register neither (buzz/ntfy style probes that already gate on credentials).
"""

from types import SimpleNamespace
from unittest.mock import patch

import hermes_cli.status as status_mod


class _FakeRegistry:
    def __init__(self, entries):
        self._entries = entries

    def plugin_entries(self):
        return self._entries


def _entry(label, *, is_connected=None, validate_config=None, check_fn=lambda: True):
    return SimpleNamespace(
        label=label,
        is_connected=is_connected,
        validate_config=validate_config,
        check_fn=check_fn,
    )


def _run_status(capsys, entries, monkeypatch):
    monkeypatch.setattr(
        "gateway.platform_registry.platform_registry", _FakeRegistry(entries)
    )
    # The import inside the plugin loop resolves through sys.modules; patch
    # the registry symbol on the real module too.
    import gateway.platform_registry as real_registry

    monkeypatch.setattr(real_registry, "platform_registry", _FakeRegistry(entries))

    status_mod.show_status(SimpleNamespace(deep=False))
    return capsys.readouterr().out


class TestPluginPlatformStatus:
    def test_dependency_probe_alone_never_means_configured(self, capsys, monkeypatch):
        """check_fn=True (SDK importable) with no credential slots must
        report NOT configured (#96190)."""
        out = _run_status(
            capsys,
            [_entry("Teams", check_fn=lambda: True)],
            monkeypatch,
        )
        line = next(l for l in out.splitlines() if "Teams" in l)
        assert "not configured" in line

    def test_validate_config_drives_configured(self, capsys, monkeypatch):
        """With validate_config registered, its verdict is the answer."""
        entries = [
            _entry("ProbeFalse", validate_config=lambda cfg: False, check_fn=lambda: True),
            _entry("ProbeTrue", validate_config=lambda cfg: True, check_fn=lambda: True),
        ]
        out = _run_status(capsys, entries, monkeypatch)
        line_f = next(l for l in out.splitlines() if "ProbeFalse" in l)
        line_t = next(l for l in out.splitlines() if "ProbeTrue" in l)
        assert "not configured" in line_f
        assert "configured" in line_t
        assert "not configured" not in line_t

    def test_is_connected_takes_priority_over_validate(self, capsys, monkeypatch):
        entries = [
            _entry(
                "ConnOK",
                is_connected=lambda cfg: True,
                validate_config=lambda cfg: False,
                check_fn=lambda: False,
            )
        ]
        out = _run_status(capsys, entries, monkeypatch)
        line = next(l for l in out.splitlines() if "ConnOK" in l)
        assert "configured" in line
        assert "not configured" not in line

    def test_no_semantic_slots_fails_to_not_configured(self, capsys, monkeypatch):
        """An entry registering neither is_connected nor validate_config is
        reported not configured — check_fn's credential semantics are
        unknowable at this call site, and every current plugin registers
        both slots."""
        entries = [
            _entry("Bare", check_fn=lambda: False),
        ]
        out = _run_status(capsys, entries, monkeypatch)
        line = next(l for l in out.splitlines() if "Bare" in l)
        assert "not configured" in line

    def test_raising_probe_fails_closed_per_entry(self, capsys, monkeypatch):
        entries = [
            _entry("Bad", validate_config=lambda cfg: (_ for _ in ()).throw(ValueError("boom"))),
            _entry("Good", validate_config=lambda cfg: True),
        ]
        out = _run_status(capsys, entries, monkeypatch)
        bad = next(l for l in out.splitlines() if "Bad" in l)
        good = next(l for l in out.splitlines() if "Good" in l)
        assert "not configured" in bad
        assert "configured" in good
