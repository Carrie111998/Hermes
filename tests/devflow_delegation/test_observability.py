import importlib.util
import sqlite3
from pathlib import Path


def test_observability_source_supports_bundled_and_sibling_layouts(tmp_path):
    bundled_root = tmp_path / "bundle"
    bundled_test = bundled_root / "tests" / "devflow_delegation" / "test_observability.py"
    bundled_script = bundled_root / "profiles" / "main" / "scripts" / "devflow_observability.py"
    bundled_script.parent.mkdir(parents=True)
    bundled_script.touch()

    shared_root = tmp_path / ".hermes"
    shared_test = shared_root / "agent-src" / "tests" / "devflow_delegation" / "test_observability.py"
    shared_script = shared_root / "profiles" / "main" / "scripts" / "devflow_observability.py"
    shared_script.parent.mkdir(parents=True)
    shared_script.touch()

    assert _observability_source(bundled_test) == bundled_script
    assert _observability_source(shared_test) == shared_script


def _observability_source(test_file: Path) -> Path:
    relative = Path("profiles") / "main" / "scripts" / "devflow_observability.py"
    for root in (test_file.parents[2], test_file.parents[3]):
        source = root / relative
        if source.is_file():
            return source
    raise FileNotFoundError(f"cannot locate deployed observability script from {test_file}")


def _load_observability(tmp_path):
    source = _observability_source(Path(__file__).resolve())
    spec = importlib.util.spec_from_file_location("devflow_observability_test", source)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    module.HERMES_ROOT = tmp_path
    module.DEVFLOW_DIR = tmp_path / "devflow"
    module.MAILBOX = tmp_path / "mailbox"
    return module


def _ledger_with_stage2_data(root):
    db = root / "devflow" / "delegation_ledger.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE requests (
            request_id TEXT PRIMARY KEY, envelope_json TEXT NOT NULL, state TEXT NOT NULL,
            source_agent TEXT NOT NULL, source_kind TEXT NOT NULL, target_repo TEXT NOT NULL,
            target_subsystem TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE leases (
            request_id TEXT PRIMARY KEY, lease_id TEXT NOT NULL, holder TEXT NOT NULL,
            acquired_at TEXT NOT NULL, expires_at TEXT NOT NULL, heartbeat_at TEXT,
            worktree_path TEXT, branch TEXT, attempt_count INTEGER
        );
        CREATE TABLE artifacts (
            id INTEGER PRIMARY KEY, request_id TEXT NOT NULL, kind TEXT NOT NULL,
            ref TEXT NOT NULL, created_at TEXT NOT NULL
        );
    """)
    envelope = ('{"source":{"agent":"operator","finding_id":"fixture-1"},'
                '"target":{"repo":"fixture","subsystem":"src"},'
                '"priority":"P3","severity":"low","title":"Synthetic fixture",'
                '"acceptance_criteria":["verified"]}')
    con.execute(
        "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        ("dwr_fixture", envelope, "BUILDING", "operator", "explicit", "fixture", "src", "task", "low",
         "2026-08-08T00:00:00+00:00", "2026-08-08T00:00:00+00:00"),
    )
    con.execute(
        "INSERT INTO leases VALUES (?,?,?,?,?,?,?,?,?)",
        ("dwr_fixture", "lse_fixture", "ddp.executor", "2026-08-08T00:00:00+00:00",
         "2026-08-08T01:00:00+00:00", "2026-08-08T00:10:00+00:00", "/tmp/ddp", "ddp-fixture-a1", 1),
    )
    con.execute(
        "INSERT INTO artifacts VALUES (?,?,?,?,?)",
        (1, "dwr_fixture", "pr", "https://example.test/pr/42", "2026-08-08T00:20:00+00:00"),
    )
    con.execute(
        "INSERT INTO artifacts VALUES (?,?,?,?,?)",
        (2, "dwr_fixture", "autonomy_gate", "shadow:merge:low:shadow_mode:abcd1234", "2026-08-08T00:21:00+00:00"),
    )
    con.commit()
    con.close()


def test_gather_surfaces_stage2_lease_and_artifacts(tmp_path):
    module = _load_observability(tmp_path)
    _ledger_with_stage2_data(tmp_path)

    gathered = module.gather()

    assert gathered["leases"][0]["holder"] == "ddp.executor"
    assert gathered["leases"][0]["branch"] == "ddp-fixture-a1"
    assert gathered["artifacts"]["dwr_fixture"][0]["ref"] == "https://example.test/pr/42"
    assert gathered["autonomy_decisions"] == [{
        "request_id": "dwr_fixture",
        "mode": "shadow",
        "action": "merge",
        "tier": "low",
        "reason": "shadow_mode",
        "created_at": "2026-08-08T00:21:00+00:00",
    }]
    page = module.render(gathered)
    assert "ddp-fixture-a1" in page
    assert "https://example.test/pr/42" in page
    assert "Autonomy gate decisions" in page
    assert "shadow_mode" in page


def test_directory_at_autonomy_sentinel_path_is_not_reported_enabled(tmp_path):
    module = _load_observability(tmp_path)
    (tmp_path / "devflow" / ".autonomy_enabled").mkdir(parents=True)

    gathered = module.gather()

    assert gathered["autonomy_flag"] is False
    assert gathered["autonomy_sentinel_note"] == "invalid (not a file)"


def test_lease_schema_failure_is_visible_not_silently_blank(tmp_path):
    module = _load_observability(tmp_path)
    db = tmp_path / "devflow" / "delegation_ledger.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE requests (state TEXT)")
    con.commit()
    con.close()

    gathered = module.gather()

    assert gathered["lease_error"] is not None
    assert "ledger read failed" in gathered["lease_error"]


def test_gather_surfaces_live_gateway_imports(tmp_path):
    module = _load_observability(tmp_path)
    devflow = tmp_path / "devflow"
    devflow.mkdir()
    (devflow / "allowlist.json").write_text(
        '{"targets":{"hermes":{"live_gateway_imports":true},"fixture":{}}}',
        encoding="utf-8",
    )

    gathered = module.gather()

    assert gathered["live_gateway"] == {"hermes": True}
    assert "hermes" in module.render(gathered)
    assert "never auto-deploy" in module.render(gathered)


def test_render_classifies_expired_lease_and_separates_metrics(tmp_path):
    module = _load_observability(tmp_path)
    _ledger_with_stage2_data(tmp_path)
    mailbox = tmp_path / "mailbox" / "devflow" / "inbox"
    mailbox.mkdir(parents=True)
    (mailbox / "legacy.json").write_text(
        '{"type":"DEVFLOW_FIX_REQUEST","from":"legacy","timestamp":"2026-08-08T00:00:00+00:00",'
        '"payload":{"issue":"legacy-1","priority":"high","task":"legacy task"}}',
        encoding="utf-8",
    )

    page = module.render(module.gather())

    assert "EXPIRED" in page
    assert "DDP ledger" in page
    assert "Legacy mailbox" in page


def test_render_surfaces_shadow_gate_command_template_and_live_polling(tmp_path):
    module = _load_observability(tmp_path)
    _ledger_with_stage2_data(tmp_path)

    page = module.render(module.gather())

    assert "python -m devflow_delegation.cli gate" in page
    assert "--changed-path &lt;paths&gt;" in page
    assert "/static/hermes-live.js" in page
    assert 'hermesLive.poll("/api/devflow"' in page
    assert 'id="pipeline-strip"' in page
    assert 'id="live-ts"' in page


def test_main_emits_live_artifact_manifest(tmp_path, monkeypatch):
    module = _load_observability(tmp_path)
    calls = {}

    def emit(html, **kwargs):
        calls["html"] = html
        calls.update(kwargs)
        return "devflow-mission-control"

    monkeypatch.setattr(module.hermes_artifacts, "emit", emit)

    assert module.main() == 0
    assert calls["refresh_secs"] == 60
    assert calls["data_endpoints"] == ["/api/devflow"]
    assert "/api/devflow" in calls["html"]


def test_render_keeps_live_lease_table_when_empty(tmp_path):
    module = _load_observability(tmp_path)

    page = module.render(module.gather())

    assert 'id="leases-tbody"' in page
    assert "No active leases" in page


def test_render_wires_live_request_detail_and_side_states(tmp_path):
    module = _load_observability(tmp_path)

    page = module.render(module.gather())

    assert 'id="ddp-live-requests"' in page
    assert 'id="ddp-live-detail"' in page
    assert "request_id=" in page
    assert "SIDE_STATES" in page
    assert "last successful ledger read" in page.lower()
    assert "Human decision history" in page
    assert "detail.human_decisions" in page
