"""Behavior tests for the optional extension registry and doctor health view."""

from hermes_cli.config_defaults import DEFAULT_CONFIG


def test_extension_registry_defaults_are_review_gated_and_complete():
    registry = DEFAULT_CONFIG["extensions"]["registry"]

    assert {
        "planning-with-files",
        "delegate-skills",
        "rtk",
        "mantis",
        "agent-reach",
        "skill-retrieval",
    } <= set(registry)
    for entry in registry.values():
        assert {
            "repo",
            "ref",
            "version",
            "scope",
            "capabilities",
            "promotion",
            "rollback",
        } <= set(entry)
        assert entry["repo"].startswith("https://github.com/")
        assert entry["ref"] == ""
        assert entry["version"] == ""
        assert entry["promotion"]["state"] == "disabled"
        assert entry["promotion"]["review_receipt"] == ""
        assert entry["rollback"]["strategy"] == "disable"
        assert "latest" not in repr(entry).lower()


def test_extension_health_defaults_report_optional_extensions_without_failing(tmp_path):
    from hermes_cli.extension_health import collect_extension_health

    rows = collect_extension_health(
        DEFAULT_CONFIG,
        hermes_home=tmp_path,
        which=lambda _command: None,
    )
    by_label = {row.label: row for row in rows}

    assert {
        "OMH",
        "Skill Retrieval",
        "RTK",
        "Planning Files",
        "Planning → Plane",
        "Delegate adapters",
        "Agent Reach",
        "Mantis",
        "Profile capability drift",
        "Reviewer raw evidence",
        "Production cron",
    } <= set(by_label)
    assert by_label["Skill Retrieval"].status == "warn"
    assert "unconfigured" in by_label["Skill Retrieval"].detail
    assert "top_k=8" in by_label["Skill Retrieval"].detail
    assert by_label["RTK"].status == "warn"
    assert "raw bypass=available" in by_label["RTK"].detail
    assert by_label["Production cron"].status == "ok"
    assert "unaffected" in by_label["Production cron"].detail


def test_extension_health_reports_configured_local_state(tmp_path):
    from copy import deepcopy

    from hermes_cli.extension_health import collect_extension_health

    config = deepcopy(DEFAULT_CONFIG)
    extensions = config["extensions"]
    extensions["registry"]["skill-retrieval"]["promotion"]["state"] = "canary"
    extensions["health"]["skill_retrieval"]["index_path"] = "indexes/skills.json"
    (tmp_path / "indexes").mkdir()
    (tmp_path / "indexes" / "skills.json").write_text("{}", encoding="utf-8")
    extensions["registry"]["planning-with-files"]["promotion"]["state"] = "canary"
    extensions["health"]["planning_files"]["current_plan"] = "plans/current.md"
    (tmp_path / "plans").mkdir()
    (tmp_path / "plans" / "current.md").write_text("# Current", encoding="utf-8")
    extensions["health"]["planning_plane"]["status"] = "synced"
    extensions["health"]["planning_plane"]["projection_hash"] = "sha256:abc"
    extensions["health"]["delegate"]["adapters"] = ["codex", "claude"]
    extensions["health"]["profile_capabilities"] = {
        "expected": ["planning.files", "terminal.raw"],
        "actual": ["planning.files", "terminal.raw"],
    }

    rows = collect_extension_health(
        config,
        hermes_home=tmp_path,
        which=lambda command: f"/usr/bin/{command}",
    )
    by_label = {row.label: row for row in rows}

    assert by_label["Skill Retrieval"].status == "ok"
    assert "index=ready" in by_label["Skill Retrieval"].detail
    assert by_label["Planning Files"].status == "ok"
    assert "plans/current.md" in by_label["Planning Files"].detail
    assert by_label["Planning → Plane"].status == "ok"
    assert "sha256:abc" in by_label["Planning → Plane"].detail
    assert by_label["Delegate adapters"].status == "ok"
    assert "codex, claude" in by_label["Delegate adapters"].detail
    assert by_label["Profile capability drift"].status == "ok"


def test_doctor_extension_health_renderer_uses_non_failing_rows(monkeypatch, tmp_path):
    import hermes_cli.doctor as doctor
    from hermes_cli.extension_health import ExtensionHealthRow

    monkeypatch.setattr(
        "hermes_cli.extension_health.collect_extension_health",
        lambda *_args, **_kwargs: [
            ExtensionHealthRow("ready", "ok", "configured"),
            ExtensionHealthRow("optional", "warn", "unconfigured"),
            ExtensionHealthRow("note", "info", "read-only"),
        ],
    )
    seen = []
    monkeypatch.setattr(doctor, "check_ok", lambda label, detail="": seen.append(("ok", label, detail)))
    monkeypatch.setattr(doctor, "check_warn", lambda label, detail="": seen.append(("warn", label, detail)))
    monkeypatch.setattr(doctor, "check_info", lambda text: seen.append(("info", text, "")))

    doctor._report_extension_health(config={}, hermes_home=tmp_path)

    assert seen == [
        ("ok", "ready", "configured"),
        ("warn", "optional", "unconfigured"),
        ("info", "note: read-only", ""),
    ]
