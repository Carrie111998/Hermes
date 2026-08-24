"""Spec 042 §5 phase — the ``workflow_ref`` resolver library.

Unit tests for ``hermes_cli.workflow_resolver`` against FIXTURE catalogs
in tmp_path — no umbrella checkout required. Covers: catalog location
(explicit path / env pin / walk-up / honest absence), strict load
validation (envelope, per-row keys, closed enums, duplicate keys),
resolution posture (None binding → None, unknown ref → clean
UnknownWorkflowError naming the ref and the known keys), args_schema
validation (required / type / enum / pattern / defaults / copy
isolation), and the ``resolve_task`` glue against real kanban tasks.

The drift guard diffing this resolver's vocabulary against the TRACKED
catalog and the umbrella filesystem lives in
``test_workflow_catalog_drift.py``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import workflow_resolver as wr


_BETA_SCHEMA = {
    "type": "object",
    "required": ["pr"],
    "properties": {
        "pr": {"type": "integer"},
        "post": {"type": "boolean", "default": False},
        "mode": {"type": "string", "enum": ["quick", "full"]},
        "date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
    },
}


def _row(key: str, **over):
    row = {
        "key": key,
        "kind": "script",
        "dialect": "hermes-py",
        "runner_affinity": ["hermes"],
        "resolution": "tracked-path",
        "description": f"Fixture row for {key}.",
        "args_schema": {},
        "capabilities": {
            "launchable": True,
            "has_turn_cap": True,
            "has_cost": True,
            "gate_kind": "none",
            "needs_worktree": False,
        },
        "honors": "enforced",
        "source": "workflows/hermes/fixture.py:1",
    }
    row.update(over)
    return row


def _write_catalog(path: Path, rows: list[dict], **envelope_over) -> Path:
    envelope = {"schema": 1, "updatedAt": "2026-08-24", "workflows": rows}
    envelope.update(envelope_over)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelope, indent=2))
    return path


@pytest.fixture
def catalog_path(tmp_path) -> Path:
    return _write_catalog(
        tmp_path / "contracts" / "workflows.json",
        [
            _row("alpha"),
            _row(
                "beta",
                dialect="claude-js",
                resolution="name-ladder",
                runner_affinity=["claude"],
                args_schema=dict(_BETA_SCHEMA),
            ),
        ],
    )


@pytest.fixture
def catalog(catalog_path) -> wr.Catalog:
    return wr.load_catalog(catalog_path)


# ---------------------------------------------------------------------------
# Location
# ---------------------------------------------------------------------------


def test_load_explicit_path(catalog_path):
    cat = wr.load_catalog(catalog_path)
    assert cat.keys() == ["alpha", "beta"]
    assert cat.path == catalog_path


def test_env_pin(monkeypatch, catalog_path):
    monkeypatch.setenv(wr.CATALOG_ENV_VAR, str(catalog_path))
    assert wr.default_catalog_path() == catalog_path


def test_env_pin_to_missing_file_is_an_error(tmp_path, monkeypatch):
    monkeypatch.setenv(wr.CATALOG_ENV_VAR, str(tmp_path / "nope.json"))
    with pytest.raises(wr.WorkflowCatalogError, match="not a file"):
        wr.default_catalog_path()


def test_walk_up_finds_umbrella_layout(tmp_path):
    catalog_file = _write_catalog(
        tmp_path / "contracts" / "workflows.json", [_row("alpha")]
    )
    deep = tmp_path / "harness" / "hermes" / "engine" / "hermes_cli"
    deep.mkdir(parents=True)
    assert wr.find_catalog(deep) == catalog_file


def test_walk_up_absent_returns_none(tmp_path):
    lone = tmp_path / "standalone" / "engine" / "hermes_cli"
    lone.mkdir(parents=True)
    assert wr.find_catalog(lone) is None


def test_default_path_without_catalog_is_honest_error(tmp_path, monkeypatch):
    monkeypatch.delenv(wr.CATALOG_ENV_VAR, raising=False)
    monkeypatch.setattr(wr, "find_catalog", lambda start=None: None)
    with pytest.raises(wr.WorkflowCatalogError, match="no workflow catalog"):
        wr.default_catalog_path()


# ---------------------------------------------------------------------------
# Load validation — the ONE place constraints live
# ---------------------------------------------------------------------------


def test_load_rejects_non_json(tmp_path):
    path = _write_catalog(tmp_path / "c.json", [_row("alpha")])
    path.write_text("{nope")
    with pytest.raises(wr.WorkflowCatalogError, match="not valid JSON"):
        wr.load_catalog(path)


def test_load_rejects_missing_file(tmp_path):
    with pytest.raises(wr.WorkflowCatalogError, match="does not exist"):
        wr.load_catalog(tmp_path / "absent.json")


def test_load_rejects_wrong_schema_version(tmp_path):
    path = _write_catalog(tmp_path / "c.json", [_row("alpha")], schema=2)
    with pytest.raises(wr.WorkflowCatalogError, match="schema 2"):
        wr.load_catalog(path)


def test_load_rejects_non_list_workflows(tmp_path):
    path = _write_catalog(tmp_path / "c.json", [_row("alpha")], workflows={})
    with pytest.raises(wr.WorkflowCatalogError, match="must be a list"):
        wr.load_catalog(path)


def test_load_rejects_row_missing_keys(tmp_path):
    row = _row("alpha")
    del row["description"]
    path = _write_catalog(tmp_path / "c.json", [row])
    with pytest.raises(wr.WorkflowCatalogError, match="missing keys: description"):
        wr.load_catalog(path)


@pytest.mark.parametrize(
    "field_name,bad",
    [
        ("kind", "macro"),
        ("dialect", "cobol"),
        ("resolution", "vibes"),
        ("honors", "sometimes"),
    ],
)
def test_load_rejects_unknown_enum_values(tmp_path, field_name, bad):
    path = _write_catalog(tmp_path / "c.json", [_row("alpha", **{field_name: bad})])
    with pytest.raises(wr.WorkflowCatalogError, match="row 'alpha'"):
        wr.load_catalog(path)


def test_load_rejects_bad_runner_affinity(tmp_path):
    path = _write_catalog(
        tmp_path / "c.json", [_row("alpha", runner_affinity=["z80"])]
    )
    with pytest.raises(wr.WorkflowCatalogError, match="runner_affinity"):
        wr.load_catalog(path)


def test_load_rejects_empty_runner_affinity(tmp_path):
    path = _write_catalog(tmp_path / "c.json", [_row("alpha", runner_affinity=[])])
    with pytest.raises(wr.WorkflowCatalogError, match="runner_affinity"):
        wr.load_catalog(path)


def test_load_rejects_bad_gate_kind(tmp_path):
    caps = _row("alpha")["capabilities"]
    caps["gate_kind"] = "maybenow"
    path = _write_catalog(tmp_path / "c.json", [_row("alpha", capabilities=caps)])
    with pytest.raises(wr.WorkflowCatalogError, match="gate_kind"):
        wr.load_catalog(path)


def test_load_rejects_non_bool_capability(tmp_path):
    caps = _row("alpha")["capabilities"]
    caps["launchable"] = "yes"
    path = _write_catalog(tmp_path / "c.json", [_row("alpha", capabilities=caps)])
    with pytest.raises(wr.WorkflowCatalogError, match="launchable"):
        wr.load_catalog(path)


def test_load_rejects_capability_missing_key(tmp_path):
    caps = _row("alpha")["capabilities"]
    del caps["needs_worktree"]
    path = _write_catalog(tmp_path / "c.json", [_row("alpha", capabilities=caps)])
    with pytest.raises(wr.WorkflowCatalogError, match="needs_worktree"):
        wr.load_catalog(path)


def test_load_rejects_non_object_args_schema(tmp_path):
    path = _write_catalog(tmp_path / "c.json", [_row("alpha", args_schema=[])])
    with pytest.raises(wr.WorkflowCatalogError, match="args_schema"):
        wr.load_catalog(path)


def test_load_rejects_source_without_line(tmp_path):
    path = _write_catalog(
        tmp_path / "c.json", [_row("alpha", source="workflows/sppcrt.js")]
    )
    with pytest.raises(wr.WorkflowCatalogError, match="path:line"):
        wr.load_catalog(path)


def test_load_rejects_duplicate_keys(tmp_path):
    path = _write_catalog(tmp_path / "c.json", [_row("alpha"), _row("alpha")])
    with pytest.raises(wr.WorkflowCatalogError, match="duplicate.*'alpha'"):
        wr.load_catalog(path)


def test_load_rejects_empty_description(tmp_path):
    path = _write_catalog(tmp_path / "c.json", [_row("alpha", description="  ")])
    with pytest.raises(wr.WorkflowCatalogError, match="description"):
        wr.load_catalog(path)


# ---------------------------------------------------------------------------
# Resolution posture
# ---------------------------------------------------------------------------


def test_resolve_known_ref(catalog):
    row = wr.resolve("alpha", catalog)
    assert row is not None and row.key == "alpha"
    assert row.launchable is True
    assert row.gate_kind == "none"
    assert row.source_path == "workflows/hermes/fixture.py"
    assert row.source_line == 1


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_resolve_blank_ref_is_no_binding(catalog, blank):
    assert wr.resolve(blank, catalog) is None


def test_resolve_unknown_ref_fails_cleanly(catalog):
    """The clean failure the validate-work-type incident never had: a
    named exception carrying the ref AND the catalog's known keys — never
    a warn-drop."""
    with pytest.raises(wr.UnknownWorkflowError) as ei:
        wr.resolve("does-not-exist", catalog)
    exc = ei.value
    assert exc.ref == "does-not-exist"
    assert exc.available == ["alpha", "beta"]
    assert "'does-not-exist'" in str(exc)
    assert "alpha" in str(exc) and "beta" in str(exc)


def test_unknown_ref_is_a_catalog_error(catalog):
    with pytest.raises(wr.WorkflowCatalogError):
        wr.resolve("nope", catalog)


def test_catalog_get_round_trips_every_row(catalog):
    for key in catalog.keys():
        assert catalog.get(key).key == key
    assert len(catalog) == 2
    assert "alpha" in catalog


# ---------------------------------------------------------------------------
# args_schema validation
# ---------------------------------------------------------------------------


def test_args_empty_schema_accepts_any_object():
    assert wr.validate_args({}, {"anything": 1}, "alpha") == {"anything": 1}


def test_args_defaults_applied():
    effective = wr.validate_args(_BETA_SCHEMA, {"pr": 7}, "beta")
    assert effective == {"pr": 7, "post": False}


def test_args_missing_required_fails():
    with pytest.raises(wr.WorkflowArgsError, match="missing required key 'pr'"):
        wr.validate_args(_BETA_SCHEMA, {}, "beta")


def test_args_type_mismatch_fails():
    with pytest.raises(wr.WorkflowArgsError, match="'pr' must be integer"):
        wr.validate_args(_BETA_SCHEMA, {"pr": "7"}, "beta")


def test_args_bool_is_not_an_integer():
    with pytest.raises(wr.WorkflowArgsError, match="'pr' must be integer"):
        wr.validate_args(_BETA_SCHEMA, {"pr": True}, "beta")


def test_args_enum_violation_fails():
    with pytest.raises(wr.WorkflowArgsError, match="'mode' must be one of"):
        wr.validate_args(_BETA_SCHEMA, {"pr": 1, "mode": "sideways"}, "beta")


def test_args_pattern_violation_fails():
    with pytest.raises(wr.WorkflowArgsError, match="'date' must match"):
        wr.validate_args(_BETA_SCHEMA, {"pr": 1, "date": "24/08/2026"}, "beta")


def test_args_undeclared_keys_pass_through():
    effective = wr.validate_args(_BETA_SCHEMA, {"pr": 1, "extra": "ok"}, "beta")
    assert effective["extra"] == "ok"


def test_args_returns_a_copy():
    original = {"pr": 1}
    effective = wr.validate_args(_BETA_SCHEMA, original, "beta")
    assert "post" in effective and "post" not in original


def test_args_non_object_fails():
    with pytest.raises(wr.WorkflowArgsError, match="must be a JSON object"):
        wr.validate_args({}, ["not", "an", "object"], "alpha")


# ---------------------------------------------------------------------------
# resolve_task — the tasks-table glue
# ---------------------------------------------------------------------------


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "profiles" / "elias").mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def test_resolve_task_unbound_card_is_none(kanban_home, catalog):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="classic free-prompt worker")
        task = kb.get_task(conn, task_id)
    assert wr.resolve_task(task, catalog) is None


def test_resolve_task_bound_card(kanban_home, catalog):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="bound", workflow_ref="beta", workflow_args='{"pr": 42}'
        )
        task = kb.get_task(conn, task_id)
    resolved = wr.resolve_task(task, catalog)
    assert resolved is not None
    assert resolved.row.key == "beta"
    assert resolved.args == {"pr": 42, "post": False}


def test_resolve_task_unknown_ref_fails_cleanly(kanban_home, catalog):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(conn, title="typo", workflow_ref="vualt-morning")
        task = kb.get_task(conn, task_id)
    with pytest.raises(wr.UnknownWorkflowError, match="vualt-morning"):
        wr.resolve_task(task, catalog)


def test_resolve_task_args_schema_violation(kanban_home, catalog):
    with kb.connect_closing() as conn:
        task_id = kb.create_task(
            conn, title="bad args", workflow_ref="beta", workflow_args="{}"
        )
        task = kb.get_task(conn, task_id)
    with pytest.raises(wr.WorkflowArgsError, match="missing required key 'pr'"):
        wr.resolve_task(task, catalog)
