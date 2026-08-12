import importlib.util
import json
import re
import sqlite3
import subprocess
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


def test_observability_source_resolves_from_a_nested_worktree(tmp_path):
    """A git worktree sits deeper than the two layouts above.

    Checkouts under `<hermes>/agent-src/.claude/worktrees/<name>` put the
    script five hops up, not three or four, so a fixed-hop lookup raises
    FileNotFoundError and takes all 22 tests in this file down with it —
    which reads as a regression when it is only a verification location.
    """
    hermes_root = tmp_path / ".hermes"
    worktree_test = (
        hermes_root
        / "agent-src"
        / ".claude"
        / "worktrees"
        / "some-worktree"
        / "tests"
        / "devflow_delegation"
        / "test_observability.py"
    )
    script = hermes_root / "profiles" / "main" / "scripts" / "devflow_observability.py"
    script.parent.mkdir(parents=True)
    script.touch()

    assert _observability_source(worktree_test) == script


def _observability_source(test_file: Path) -> Path:
    """Locate the deployed observability script above *test_file*.

    Anchored on the marker path rather than a fixed number of parent hops:
    the bundled layout puts it 3 hops up and the `<hermes>/agent-src` sibling
    layout 4, but a git worktree is deeper still. Walking the ancestors
    covers all three and any future checkout depth. Compare the same fix in
    01cf92357 (Hermes-root paths) and 3b9612474 (SOUL/SKILL paths).
    """
    relative = Path("profiles") / "main" / "scripts" / "devflow_observability.py"
    for root in test_file.parents:
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


def _ledger_with_triaged_approvals(root):
    db = root / "devflow" / "delegation_ledger.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE requests (request_id TEXT PRIMARY KEY, envelope_json TEXT NOT NULL, state TEXT NOT NULL, "
        "source_agent TEXT NOT NULL, source_kind TEXT NOT NULL, target_repo TEXT NOT NULL, "
        "target_subsystem TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL, "
        "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
    )
    con.executemany(
        "INSERT INTO requests VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "dwr_100",
                json.dumps({
                    "source": {"agent": "operator", "finding_id": "finding-oldest"},
                    "target": {"repo": "fixture", "subsystem": "src"},
                    "title": "Oldest approval", "acceptance_criteria": ["verified"],
                }),
                "TRIAGED", "operator", "explicit", "fixture", "src", "task", "low",
                "2026-08-08T08:00:00+00:00", "2026-08-08T08:00:00+00:00",
            ),
            (
                "dwr_200",
                json.dumps({
                    "source": {"agent": "operator", "finding_id": "finding-newest"},
                    "target": {"repo": "fixture", "subsystem": "src"},
                    "title": "Newest approval", "acceptance_criteria": ["verified"],
                }),
                "TRIAGED", "operator", "explicit", "fixture", "src", "task", "low",
                "2026-08-08T09:00:00+00:00", "2026-08-08T09:00:00+00:00",
            ),
        ],
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


def test_render_live_poll_preserves_request_state_when_ledger_is_unavailable(tmp_path):
    module = _load_observability(tmp_path)
    page = module.render(module.gather())

    assert "if (data.ledger_available)" in page
    assert "degraded" in page
    assert "renderRequests(data.requests);" in page
    assert page.index("if (data.ledger_available)") < page.index("renderRequests(data.requests);")


def test_rendered_approval_queue_uses_request_ids_and_newest_first(tmp_path):
    module = _load_observability(tmp_path)
    _ledger_with_triaged_approvals(tmp_path)

    page = module.render(module.gather())
    approval_table = page.split('<tbody id="ddp-approval-tbody">', 1)[1].split("</tbody>", 1)[0]

    assert "dwr_200" in approval_table
    assert "finding-newest" not in approval_table
    assert approval_table.index("dwr_200") < approval_table.index("dwr_100")


def test_render_approval_gate_has_live_independent_queue_target(tmp_path):
    module = _load_observability(tmp_path)
    page = module.render(module.gather())

    assert 'id="ddp-approval-tbody"' in page
    assert "data.approval_queue" in page
    assert "approval_queue_page" in page
    assert "function renderApprovalQueue(items, page, awaitingCount)" in page
    assert "for item in ddp_awaiting[:200]:" in Path(module.__file__).read_text(encoding="utf-8")
    assert '"<td><code>" + escapeHtml(action)' in page
    assert page.index("if (data.ledger_available)") < page.index("renderApprovalQueue(\n        data.approval_queue")


def test_render_approval_gate_explains_platform_command_variants(tmp_path):
    module = _load_observability(tmp_path)
    page = module.render(module.gather())

    assert "/ddp-approve &lt;request-id&gt; &lt;evidence&gt; then " in page
    assert "/ddp-approve-confirm" in page
    assert "<code>/hermes ddp-approve &hellip;</code>" in page
    assert "<code>/hermes ddp-approve-confirm &hellip;</code>" in page
    assert "<code>!ddp-approve &hellip;</code>" in page
    assert "<code>!ddp-approve-confirm &hellip;</code>" in page
    assert "This page never executes" in page


def test_render_approval_gate_escapes_request_id_in_command_copy(tmp_path):
    module = _load_observability(tmp_path)
    gathered = module.gather()
    gathered["ddp_awaiting_approval"] = [{
        "request_id": "dwr_<unsafe>",
        "title": "Synthetic approval",
        "target": "fixture/src",
        "criteria": "verified",
    }]

    page = module.render(gathered)

    assert "/ddp-approve dwr_&lt;unsafe&gt; &lt;evidence&gt;" in page
    assert "/ddp-approve dwr_<unsafe> &lt;evidence&gt;" not in page


def test_render_static_approval_queue_coerces_truthy_non_string_title(tmp_path):
    module = _load_observability(tmp_path)
    gathered = module.gather()
    gathered["ddp_awaiting_approval"] = [{
        "request_id": "dwr_non_string_title",
        "title": True,
        "target": "fixture/src",
        "criteria": "verified",
    }]

    page = module.render(gathered)
    approval_table = page.split('<tbody id="ddp-approval-tbody">', 1)[1].split("</tbody>", 1)[0]

    assert "<td>True</td>" in approval_table


def test_render_static_approval_queue_matches_live_truncation_bounds(tmp_path):
    module = _load_observability(tmp_path)
    title = "t" * 161
    criteria = "c" * 221
    gathered = module.gather()
    gathered["ddp_awaiting_approval"] = [{
        "request_id": "dwr_truncated",
        "title": title,
        "target": "fixture/src",
        "criteria": criteria,
    }]

    page = module.render(gathered)
    approval_table = page.split('<tbody id="ddp-approval-tbody">', 1)[1].split("</tbody>", 1)[0]

    assert f"<td>{title[:160]}…</td>" in approval_table
    assert f"<td>{criteria[:220]}…</td>" in approval_table
    assert approval_table.count("…") == 2
    assert title not in approval_table
    assert criteria not in approval_table


def test_render_approval_gate_marks_static_display_overflow(tmp_path):
    module = _load_observability(tmp_path)
    gathered = module.gather()
    gathered["ddp_awaiting_approval"] = [
        {
            "request_id": f"dwr_{index:03d}",
            "finding_id": f"finding-{index}",
            "title": "Synthetic approval",
            "target": "fixture/src",
            "criteria": "verified",
        }
        for index in range(201)
    ]

    page = module.render(gathered)

    assert "+ 1 more awaiting approval; displayed queue is capped at 200." in page


def test_render_approval_queue_live_refresh_matches_static_display_bounds(tmp_path):
    module = _load_observability(tmp_path)
    page = module.render(module.gather())
    script = re.search(r"<script>\s*(.*?)\s*</script>", page, re.DOTALL)
    assert script is not None
    script_path = tmp_path / "devflow-canvas.js"
    script_path.write_text(script.group(1), encoding="utf-8")
    runner_path = tmp_path / "run-canvas.js"
    runner_path.write_text(
        """
const fs = require('fs');
let poll;
const body = { innerHTML: '' };
global.document = {
  getElementById: (id) => id === 'ddp-approval-tbody' ? body : null,
  addEventListener: () => {},
};
global.hermesLive = { poll: (_url, callback) => { poll = callback; } };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });
eval(fs.readFileSync(process.argv[2], 'utf8'));
poll({
  ledger_available: true,
  approval_queue: [{
    request_id: 'dwr_long', title: 't'.repeat(161),
    target_repo: 'fixture', target_subsystem: 'src', acceptance_criteria: ['c'.repeat(221)],
  }],
  approval_queue_page: { limit: 200, has_more: false }, awaiting_approval_count: 1,
  by_state: {}, ledger_freshness: {}, side_state_counts: {}, requests: [],
  active_leases: [], expired_leases: [],
});
console.log(JSON.stringify(body.innerHTML));
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(runner_path), str(script_path)], capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert "t" * 160 + "…" in rendered
    assert "c" * 220 + "…" in rendered
    assert "t" * 161 not in rendered
    assert "c" * 221 not in rendered


def test_render_approval_queue_overflow_refreshes_without_runtime_error(tmp_path):
    module = _load_observability(tmp_path)
    page = module.render(module.gather())
    script = re.search(r"<script>\s*(.*?)\s*</script>", page, re.DOTALL)
    assert script is not None
    script_path = tmp_path / "devflow-canvas.js"
    script_path.write_text(script.group(1), encoding="utf-8")
    runner_path = tmp_path / "run-canvas.js"
    runner_path.write_text(
        """
const fs = require('fs');
let poll;
const body = { innerHTML: '' };
global.document = {
  getElementById: (id) => id === 'ddp-approval-tbody' ? body : null,
  addEventListener: () => {},
};
global.hermesLive = { poll: (_url, callback) => { poll = callback; } };
global.fetch = () => Promise.resolve({ json: () => Promise.resolve({}) });
eval(fs.readFileSync(process.argv[2], 'utf8'));
poll({
  ledger_available: true,
  approval_queue: Array.from({ length: 200 }, (_value, index) => ({
    request_id: 'dwr_' + String(index).padStart(3, '0'), title: 'Synthetic approval',
    target_repo: 'fixture', target_subsystem: 'src', acceptance_criteria: ['verified'],
  })),
  approval_queue_page: { limit: 200, has_more: true },
  awaiting_approval_count: 201,
  by_state: {}, ledger_freshness: {}, side_state_counts: {}, requests: [],
  active_leases: [], expired_leases: [],
});
console.log(JSON.stringify(body.innerHTML));
""",
        encoding="utf-8",
    )

    result = subprocess.run(
        ["node", str(runner_path), str(script_path)], capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "+1 more awaiting approval; displayed queue is capped at 200." in json.loads(result.stdout)


def test_rendered_canvas_script_passes_node_syntax_check(tmp_path):
    module = _load_observability(tmp_path)
    page = module.render(module.gather())
    script = re.search(r"<script>\s*(.*?)\s*</script>", page, re.DOTALL)
    assert script is not None
    script_path = tmp_path / "devflow-canvas.js"
    script_path.write_text(script.group(1), encoding="utf-8")

    result = subprocess.run(
        ["node", "--check", str(script_path)], capture_output=True, text=True, check=False,
    )

    assert result.returncode == 0, result.stderr


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


def test_render_includes_safe_auth_and_tick_health_containers(tmp_path):
    module = _load_observability(tmp_path)
    page = module.render(module.gather())

    assert 'id="ddp-auth-health"' in page
    assert 'id="ddp-tick-health"' in page
    assert "ddp_auth_readiness" in page
    assert "tick_health" in page


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
    assert "transition.actor" not in page
    assert "entry.actor" not in page


def test_legacy_mailbox_approval_section_is_audit_only(tmp_path):
    module = _load_observability(tmp_path)
    gathered = module.gather()
    gathered["awaiting"] = [{
        "sr_code": "SR-1",
        "priority": "high",
        "component": "fixture",
        "problem": "Synthetic legacy approval",
        "reversibility": "yes",
        "age": "1h",
    }]

    page = module.render(gathered)

    assert "historical records" in page.lower()
    assert "do not reply to them for lifecycle transitions" in page.lower()
    assert "Historical record — use the canonical DDP approval gate above." in page
    assert "Reply to the matching DEVFLOW_APPROVAL_REQUEST" not in page
    assert "(reply to the DEVFLOW_APPROVAL_REQUEST)" not in page
