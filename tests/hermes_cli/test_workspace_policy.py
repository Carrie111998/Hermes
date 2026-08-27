"""Per-board workspace policy — the M3a confinement contract assertion set.

SCOPE: protects against **accidental escape and cooperative execution**, not
arbitrary malicious same-UID activity. Every assertion is a path predicate, a
file check, or a string match — none is an OS-level sandbox. A process running
as your user can `chdir` after launch, rewrite the config these checks read, or
write `kanban.db` directly.

All fixtures here are disposable `tmp_path` trees. Nothing touches a real
repository, credential, Hermes configuration, or external service.

WHAT MAKES THIS SUITE MEANINGFUL: `open` mode is the default, so a test that
only exercised the default would prove nothing about the contract. Every
contract assertion below is tested in `sandbox` mode, and
`test_open_mode_does_not_satisfy_the_contract` pins the difference.
"""

import os
import subprocess

import pytest

from hermes_cli import workspace_policy as wp
from hermes_cli.dispatch_confinement import PreflightRefusal


def _sandbox_policy(root, **over):
    base = dict(
        board="eval", mode=wp.MODE_SANDBOX, allowed_roots=(str(root),),
        protected_paths=(), hermes_home_root=str(root),
        required_deny_globs=(), prohibited_commands=(), allowed_commands=(),
    )
    base.update(over)
    return wp.WorkspacePolicy(**base)


def _git_fixture(path, *, remote=None, alternates=False, untracked=False):
    """A disposable git repo. Never a clone of anything real."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True,
                   capture_output=True)
    (path / "src.txt").write_text("content")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "init"],
                   check=True, capture_output=True)
    if remote:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", remote],
                       check=True, capture_output=True)
    if alternates:
        info = path / ".git" / "objects" / "info"
        info.mkdir(parents=True, exist_ok=True)
        (info / "alternates").write_text("/some/other/repo/.git/objects\n")
    if untracked:
        (path / "leaked_answer.txt").write_text("the fix")
    return path


# ===================== policy resolution ===================================


def test_the_default_is_open_and_says_so():
    status = wp.policy_status("unconfigured-board", config={})
    assert status["mode"] == wp.MODE_OPEN
    assert status["contract_satisfied"] is False
    assert "does NOT satisfy" in status["note"]


def test_open_mode_does_not_satisfy_the_contract(tmp_path):
    """The distinction the whole module rests on.

    An open board runs only the launch checks. Reporting that as "passed" without
    reporting what was skipped is how "configured" gets mistaken for "contained".
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    report = wp.enforce("t", str(ws), board="open-board", config={})
    assert report.mode == wp.MODE_OPEN
    assert report.contract_satisfied is False
    assert set(report.skipped) >= {"fixture-state", "hermes-home", "command-guards"}


def test_board_settings_override_the_top_level():
    cfg = {"kanban": {"workspace_policy": {
        "mode": "open",
        "boards": {"eval": {"mode": "sandbox", "allowed_roots": ["/tmp/x"]}},
    }}}
    assert wp.resolve_policy("other", config=cfg).mode == wp.MODE_OPEN
    evalp = wp.resolve_policy("eval", config=cfg)
    assert evalp.mode == wp.MODE_SANDBOX
    assert evalp.allowed_roots == ("/tmp/x",)


# ===================== assertions 1-2, 11-15: paths ========================


def test_sandbox_without_allowed_roots_refuses(tmp_path):
    """"Unspecified" must never be read as "anywhere"."""
    pol = _sandbox_policy(tmp_path, allowed_roots=())
    with pytest.raises(PreflightRefusal, match="allowed_roots"):
        wp.assert_path_authorized("t", str(tmp_path / "ws"), pol)


def test_a_workspace_outside_every_allowed_root_refuses(tmp_path):
    pol = _sandbox_policy(tmp_path / "sandbox")
    (tmp_path / "sandbox").mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(PreflightRefusal, match="outside every authorized root"):
        wp.assert_path_authorized("t", str(outside), pol)


def test_a_workspace_inside_an_allowed_root_passes(tmp_path):
    root = tmp_path / "sandbox"
    ws = root / "fixture"
    ws.mkdir(parents=True)
    wp.assert_path_authorized("t", str(ws), _sandbox_policy(root))


@pytest.mark.parametrize("subpath,pattern", [
    ("dev/visitreno", "*/dev/visitreno*"),
    ("Documents/dev/project", "*Documents/dev*"),
    (".hermes/kanban", "*.hermes*"),
    ("Documents/Main/live", "*Documents/Main*"),
])
def test_protected_paths_are_denied(tmp_path, subpath, pattern):
    """The canonical checkouts M2a's escape reached, and their neighbours."""
    target = tmp_path / subpath
    target.mkdir(parents=True)
    pol = _sandbox_policy(tmp_path, protected_paths=(pattern,))
    with pytest.raises(PreflightRefusal, match="protected path"):
        wp.assert_path_authorized("t", str(target), pol)


def test_a_symlink_cannot_smuggle_a_worker_into_a_protected_path(tmp_path):
    """The defect a reviewer demonstrated: realpath canonicalized the spelling
    but nothing asked whether the destination was authorized."""
    protected = tmp_path / "dev" / "visitreno"
    protected.mkdir(parents=True)
    innocent = tmp_path / "sandbox" / "looks-fine"
    innocent.parent.mkdir(parents=True)
    try:
        innocent.symlink_to(protected)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    pol = _sandbox_policy(tmp_path, protected_paths=("*/dev/visitreno*",))
    with pytest.raises(PreflightRefusal, match="protected path"):
        wp.assert_path_authorized("t", str(innocent), pol)


def test_a_glob_with_a_literal_space_is_rejected_as_a_footgun(tmp_path):
    """`fnmatch` is literal on whitespace: `*git push*` is evaded by
    `git   push`. A policy that ships one is refused rather than trusted."""
    pol = _sandbox_policy(tmp_path, protected_paths=("*git push*",))
    with pytest.raises(PreflightRefusal, match="literal space"):
        wp.assert_path_authorized("t", str(tmp_path), pol)


# ===================== assertions 3-6: fixture state =======================


def test_a_fixture_with_a_remote_is_refused(tmp_path):
    fx = _git_fixture(tmp_path / "fx", remote="https://example.invalid/repo.git")
    with pytest.raises(PreflightRefusal, match="remote"):
        wp.assert_fixture_state("t", str(fx), _sandbox_policy(tmp_path))


def test_an_origin_less_fixture_passes(tmp_path):
    fx = _git_fixture(tmp_path / "fx")
    checks = wp.assert_fixture_state("t", str(fx), _sandbox_policy(tmp_path))
    assert "origin-less" in checks


def test_an_alternate_object_database_is_refused(tmp_path):
    fx = _git_fixture(tmp_path / "fx", alternates=True)
    with pytest.raises(PreflightRefusal, match="alternate"):
        wp.assert_fixture_state("t", str(fx), _sandbox_policy(tmp_path))


def test_untracked_source_is_refused(tmp_path):
    """M2a's invalid comparison: the answer was reachable from the tree."""
    fx = _git_fixture(tmp_path / "fx", untracked=True)
    with pytest.raises(PreflightRefusal, match="untracked"):
        wp.assert_fixture_state("t", str(fx), _sandbox_policy(tmp_path))


@pytest.mark.parametrize("name", [".env", ".env.local", ".env.production"])
def test_secret_files_are_refused(tmp_path, name):
    fx = _git_fixture(tmp_path / "fx")
    (fx / name).write_text("TOKEN=redacted")
    with pytest.raises(PreflightRefusal, match=name):
        wp.assert_fixture_state("t", str(fx), _sandbox_policy(tmp_path))


@pytest.mark.parametrize("name", [".vercel", ".aws", ".ssh"])
def test_secret_directories_are_refused(tmp_path, name):
    fx = _git_fixture(tmp_path / "fx")
    (fx / name).mkdir()
    with pytest.raises(PreflightRefusal, match=name):
        wp.assert_fixture_state("t", str(fx), _sandbox_policy(tmp_path))


# ===================== assertions 7-8: HERMES_HOME =========================


def test_a_hermes_home_outside_the_sandbox_is_refused(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    pol = _sandbox_policy(tmp_path / "sandbox")
    (tmp_path / "sandbox").mkdir()
    with pytest.raises(PreflightRefusal, match="outside the sandbox root"):
        wp.assert_hermes_home("t", pol, home=str(outside))


def test_a_clean_sandbox_home_passes(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("kanban:\n  max_in_progress: 1\n")
    checks = wp.assert_hermes_home("t", _sandbox_policy(tmp_path), home=str(home))
    assert "no-live-key" in checks


def test_key_material_in_the_home_is_refused(tmp_path):
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text("api_key: sk-ant-EXAMPLENOTREAL0000000000\n")
    with pytest.raises(PreflightRefusal, match="live key material") as exc:
        wp.assert_hermes_home("t", _sandbox_policy(tmp_path), home=str(home))
    # Locations only — the value must never appear in the refusal.
    assert "sk-ant-EXAMPLENOTREAL0000000000" not in str(exc.value)
    assert "config.yaml" in str(exc.value)


def test_the_scanner_never_returns_a_secret_value(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    secret = "sk-or-EXAMPLENOTREAL1111111111"
    (home / "creds.json").write_text(f'{{"key": "{secret}"}}')
    findings = wp.scan_for_key_material(str(home))
    assert findings
    assert all(secret not in f for f in findings)


def test_a_forbidden_digest_matches_without_revealing_the_key(tmp_path):
    """An operator can pin a specific key; the digest never reveals it."""
    import hashlib

    home = tmp_path / "h"
    home.mkdir()
    blob = b"totally-opaque-bytes-with-no-known-prefix"
    (home / "blob.bin").write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()

    assert wp.scan_for_key_material(str(home)) == []
    findings = wp.scan_for_key_material(str(home), forbidden_sha256=(digest,))
    assert findings and "forbidden key digest" in findings[0]


# ===================== assertions 9, 10, 16-20: guards =====================


def test_single_query_mode_must_be_deny(tmp_path):
    pol = _sandbox_policy(tmp_path)
    cfg = {"approvals": {"single_query_mode": "allow"}}
    with pytest.raises(PreflightRefusal, match="single_query_mode"):
        wp.assert_command_guards("t", pol, config=cfg)


def test_single_query_deny_passes(tmp_path):
    cfg = {"approvals": {"single_query_mode": "deny"}}
    checks = wp.assert_command_guards("t", _sandbox_policy(tmp_path), config=cfg)
    assert "single_query_mode=deny" in checks


def test_missing_required_deny_globs_refuse(tmp_path):
    pol = _sandbox_policy(tmp_path, required_deny_globs=("*git*push*", "*vercel*"))
    cfg = {"approvals": {"single_query_mode": "deny", "deny": ["*vercel*"]}}
    with pytest.raises(PreflightRefusal, match="missing required globs"):
        wp.assert_command_guards("t", pol, config=cfg)


def test_a_deny_glob_with_a_literal_space_is_refused(tmp_path):
    """The real leak found in M2b: `*git push*` allowed `git   push`."""
    pol = _sandbox_policy(tmp_path, required_deny_globs=("*git push*",))
    cfg = {"approvals": {"single_query_mode": "deny", "deny": ["*git push*"]}}
    with pytest.raises(PreflightRefusal, match="literal space"):
        wp.assert_command_guards("t", pol, config=cfg)


@pytest.mark.parametrize("command", [
    "git push origin main",
    "GIT_DIR=. git   push   origin   main",
    'git pu""sh origin main',
    "cd /tmp && git push",
])
def test_the_prohibited_matrix_blocks_obfuscated_variants(tmp_path, command):
    """Whitespace, env prefixes, quote splitting and `cd x &&` chaining."""
    pol = _sandbox_policy(tmp_path, prohibited_commands=(command,))
    cfg = {"approvals": {"single_query_mode": "deny", "deny": ["*git*push*"]}}
    checks = wp.assert_command_guards("t", pol, config=cfg)
    assert "prohibited-matrix-blocked" in checks


def test_an_unblocked_prohibited_command_refuses(tmp_path):
    pol = _sandbox_policy(tmp_path, prohibited_commands=("vercel deploy",))
    cfg = {"approvals": {"single_query_mode": "deny", "deny": ["*git*push*"]}}
    with pytest.raises(PreflightRefusal, match="not blocked"):
        wp.assert_command_guards("t", pol, config=cfg)


def test_legitimate_commands_must_not_be_false_positived(tmp_path):
    pol = _sandbox_policy(tmp_path, allowed_commands=("npm test", "git status"))
    cfg = {"approvals": {"single_query_mode": "deny", "deny": ["*git*push*"]}}
    checks = wp.assert_command_guards("t", pol, config=cfg)
    assert "no-false-positives" in checks


def test_an_overbroad_deny_that_blocks_real_work_refuses(tmp_path):
    pol = _sandbox_policy(tmp_path, allowed_commands=("git status",))
    cfg = {"approvals": {"single_query_mode": "deny", "deny": ["*git*"]}}
    with pytest.raises(PreflightRefusal, match="legitimate commands"):
        wp.assert_command_guards("t", pol, config=cfg)


# ===================== the whole set, end to end ===========================


def test_a_fully_compliant_sandbox_board_passes_everything(tmp_path, monkeypatch):
    root = tmp_path / "sandbox"
    fx = _git_fixture(root / "fixtures" / "app")
    home = root / "hermes-home"
    home.mkdir(parents=True)
    (home / "config.yaml").write_text("kanban:\n  max_in_progress: 1\n")
    monkeypatch.setenv("HERMES_HOME", str(home))

    cfg = {
        "approvals": {"single_query_mode": "deny", "deny": ["*git*push*", "*vercel*"]},
        "kanban": {"workspace_policy": {"boards": {"eval": {
            "mode": "sandbox",
            "allowed_roots": [str(root)],
            "protected_paths": ["*/dev/visitreno*"],
            "hermes_home_root": str(root),
            "required_deny_globs": ["*git*push*", "*vercel*"],
            "prohibited_commands": ["git push origin main", "vercel --prod"],
            "allowed_commands": ["npm test", "git status"],
        }}}},
    }
    report = wp.enforce("t", str(fx), board="eval", config=cfg)
    assert report.contract_satisfied is True
    assert report.skipped == []
    for expected in ("path-authorized", "origin-less", "no-alternates",
                     "no-secrets", "no-untracked-source", "hermes-home-confined",
                     "no-live-key", "single_query_mode=deny", "deny-globs-present",
                     "prohibited-matrix-blocked", "no-false-positives"):
        assert expected in report.passed, f"{expected} did not run"


def test_one_broken_assertion_refuses_the_whole_dispatch(tmp_path, monkeypatch):
    """No warn tier: any failure refuses."""
    root = tmp_path / "sandbox"
    fx = _git_fixture(root / "fixtures" / "app",
                      remote="https://example.invalid/x.git")
    home = root / "hermes-home"
    home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(home))
    cfg = {"approvals": {"single_query_mode": "deny"},
           "kanban": {"workspace_policy": {"boards": {"eval": {
               "mode": "sandbox", "allowed_roots": [str(root)],
               "hermes_home_root": str(root)}}}}}
    with pytest.raises(PreflightRefusal):
        wp.enforce("t", str(fx), board="eval", config=cfg)


# ===================== C1 and C2, end to end ===============================
#
# C1: a dispatch whose preflight fails creates no task_runs row.
# C2: task_runs.observed_cwd, when recorded, is under the declared fixture root.


@pytest.fixture
def sandbox_board(tmp_path, monkeypatch):
    """A real board on a disposable tree, in sandbox mode.

    The policy is supplied by patching ``load_config`` — the same path
    production reads — rather than by stubbing ``resolve_policy``, so these
    tests exercise resolution too.
    """
    from hermes_cli import kanban_db as kb

    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    kb._INITIALIZED_PATHS.clear()
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda _n: True)

    root = tmp_path / "sandbox"
    root.mkdir()
    cfg = {
        "approvals": {"single_query_mode": "deny", "deny": ["*git*push*"]},
        "kanban": {"workspace_policy": {"boards": {"default": {
            "mode": "sandbox",
            "allowed_roots": [str(root)],
            "protected_paths": ["*/dev/visitreno*"],
            "hermes_home_root": str(tmp_path),
            "required_deny_globs": ["*git*push*"],
            # The fixture is a plain directory, not a git repo, so the
            # git-specific assertions have nothing to inspect. They are
            # exercised directly in the unit tests above.
            "require_no_untracked_source": False,
        }}}},
    }
    monkeypatch.setattr("hermes_cli.config.load_config", lambda *a, **k: cfg)
    return tmp_path, root, cfg


def _seed(conn, workspace, status="ready"):
    from hermes_cli import kanban_db as kb

    tid = kb.create_task(conn, title="w", assignee="coder",
                         workspace_kind="dir", workspace_path=str(workspace))
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, tid))
    conn.commit()
    return tid


def test_c1_a_policy_refusal_creates_no_run_row(sandbox_board, monkeypatch):
    """C1, with a policy failure rather than an unusable path.

    The workspace is perfectly usable — it is simply outside every authorized
    root. Nothing may be created for it.
    """
    from hermes_cli import kanban_db as kb

    tmp_path, root, cfg = sandbox_board
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    try:
        outside = tmp_path / "not-authorized"
        outside.mkdir()
        tid = _seed(conn, outside)

        def _never(task, workspace, **kwargs):
            pytest.fail("a worker was spawned for an unauthorized workspace")

        result = kb.dispatch_once(conn, spawn_fn=_never)

        assert tid in result.refused_confinement, result
        runs = conn.execute("SELECT COUNT(*) c FROM task_runs WHERE task_id = ?",
                            (tid,)).fetchone()["c"]
        assert runs == 0, "C1 violated: a refused dispatch created a run row"
        task = kb.get_task(conn, tid)
        assert task.status == "ready" and task.claim_lock is None
        assert task.current_run_id is None
    finally:
        conn.close()


def test_c2_a_recorded_observed_cwd_is_under_the_declared_root(sandbox_board):
    """C2, asserted against the value actually written to the database."""
    from hermes_cli import kanban_db as kb

    tmp_path, root, cfg = sandbox_board
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    procs = []
    try:
        ws = root / "fixture"
        ws.mkdir()
        tid = _seed(conn, ws)

        def _spawn(task, workspace, **kwargs):
            p = subprocess.Popen(["/bin/sh", "-c", "sleep 5"], cwd=workspace)
            procs.append(p)
            return p.pid

        result = kb.dispatch_once(conn, spawn_fn=_spawn)
        assert tid in [i for i, _, _ in result.spawned], result

        recorded = conn.execute(
            "SELECT observed_cwd FROM task_runs WHERE task_id = ?",
            (tid,)).fetchone()["observed_cwd"]
        assert recorded is not None, "a confined launch must be evidenced"
        assert wp._under(recorded, str(root)), (
            f"C2 violated: {recorded!r} is outside the declared root {str(root)!r}")
    finally:
        for p in procs:
            p.kill()
            p.wait()
        conn.close()


def test_c2_holds_for_an_escaped_launch_by_recording_nothing(sandbox_board):
    """C2 must not be satisfied by writing a false value.

    An escaped worker records no observed_cwd at all, so the invariant "every
    recorded value is under the root" stays true without ever claiming the
    escape was confined.
    """
    from hermes_cli import kanban_db as kb

    tmp_path, root, cfg = sandbox_board
    conn = kb.connect(db_path=tmp_path / "kanban.db")
    escaped = {}
    try:
        ws = root / "fixture"
        ws.mkdir()
        tid = _seed(conn, ws)

        def _escaping(task, workspace, **kwargs):
            p = subprocess.Popen(["/bin/sh", "-c", "sleep 30"], cwd="/")
            escaped["p"] = p
            return p.pid

        kb.dispatch_once(conn, spawn_fn=_escaping)

        rows = conn.execute("SELECT observed_cwd FROM task_runs WHERE task_id = ?",
                            (tid,)).fetchall()
        assert all(r["observed_cwd"] is None for r in rows)
        escaped["p"].wait(timeout=10)      # terminated, not left running
    finally:
        p = escaped.get("p")
        if p and p.poll() is None:
            p.kill()
            p.wait()
        conn.close()
