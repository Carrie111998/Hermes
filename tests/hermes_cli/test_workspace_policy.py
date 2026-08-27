"""Per-board workspace policy — the twenty-assertion registry.

SCOPE: protects against **accidental escape and cooperative execution**, not
arbitrary malicious same-UID activity. Every assertion is a path predicate, a
file check, or a call into Hermes' own guards — none is an OS-level sandbox.

All fixtures are disposable `tmp_path` trees. Nothing touches a real repository,
credential, Hermes configuration, production system, or external service.

WHAT THIS SUITE EXISTS TO PREVENT: an earlier revision reported the contract
satisfied because `mode == sandbox`, having run five checks against a directory
that was not even a git repository. `contract_satisfied` is now evidence:
twenty named assertions, each PASS/FAIL/SKIPPED, against the real workspace and
a verified launch.
"""

import json
import os
import subprocess

import pytest

from hermes_cli import workspace_policy as wp
from hermes_cli.dispatch_confinement import PreflightRefusal, VERIFIED


class _Launch:
    """Stands in for a dispatch_confinement.LaunchVerification."""

    def __init__(self, cwd, status=VERIFIED):
        self.observed_cwd = str(cwd)
        self.status = status


def _fixture(path, *, remote=None, alternates=False, untracked=False,
             attest=True):
    """A disposable git repo. Never a clone of anything real."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
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
    if attest:
        wp.build_fixture_attestation(str(path), build_source="test")
    if untracked:
        (path / "leaked_answer.txt").write_text("the fix")
    return path


def _policy(root, **over):
    base = dict(
        board="eval", mode=wp.MODE_SANDBOX,
        allowed_roots=(str(root),),
        protected_paths=("*/dev/visitreno*",),
        hermes_home_root=str(root),
        required_deny_globs=("*git*push*", "*vercel*"),
        prohibited_commands=("git push origin main", "vercel --prod"),
        allowed_commands=("npm test", "git status"),
    )
    base.update(over)
    return wp.WorkspacePolicy(**base)


GOOD_CONFIG = {"approvals": {"single_query_mode": "deny",
                             "deny": ["*git*push*", "*vercel*"]}}


@pytest.fixture
def guards(monkeypatch):
    """Drive Hermes' shipped guards from a disposable resolved config."""
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda *a, **k: GOOD_CONFIG)
    return GOOD_CONFIG


# ===================== the registry itself =================================


def test_there_are_exactly_twenty_assertions():
    assert len(wp.ASSERTIONS) == 20
    assert sorted(wp.ASSERTION_NAMES) == list(range(1, 21))
    assert wp.REQUIRED_ASSERTION_IDS == frozenset(range(1, 21))


def test_contract_satisfied_requires_every_assertion_to_pass():
    report = wp.PolicyReport(board="b", mode=wp.MODE_SANDBOX)
    for aid in range(1, 20):
        report.record(aid, wp.PASS)
    assert report.missing == {20}
    assert report.contract_satisfied is False

    report.record(20, wp.PASS)
    assert report.contract_satisfied is True


@pytest.mark.parametrize("status", [wp.FAIL, wp.SKIPPED])
def test_one_non_passing_assertion_defeats_the_contract(status):
    report = wp.PolicyReport(board="b", mode=wp.MODE_SANDBOX)
    for aid in range(1, 20):
        report.record(aid, wp.PASS)
    report.record(20, status)
    assert report.contract_satisfied is False


def test_open_mode_can_never_satisfy_the_contract():
    report = wp.PolicyReport(board="b", mode=wp.MODE_OPEN)
    for aid in range(1, 21):
        report.record(aid, wp.PASS)
    assert report.contract_satisfied is False


# ===================== status is evidence, not inference ===================


def test_status_does_not_infer_satisfaction_from_mode(tmp_path):
    """The defect: sandbox mode alone reported the contract satisfied."""
    cfg = {"kanban": {"workspace_policy": {"boards": {"eval": {
        "mode": "sandbox", "allowed_roots": [str(tmp_path)],
        "protected_paths": ["*x*"], "hermes_home_root": str(tmp_path),
        "required_deny_globs": ["*git*push*"],
        "prohibited_commands": ["git push"], "allowed_commands": ["npm test"],
    }}}}}
    status = wp.policy_status("eval", config=cfg)
    assert status["mode"] == wp.MODE_SANDBOX
    assert status["configured_ready"] is True
    assert status["contract_satisfied"] is False, (
        "satisfaction was inferred from configuration")
    assert status["evidence"] is None


def test_status_reports_evidence_when_a_run_exists():
    report = wp.PolicyReport(board="eval", mode=wp.MODE_SANDBOX)
    for aid in range(1, 21):
        report.record(aid, wp.PASS)
    status = wp.policy_status("eval", config={}, last_report=report)
    assert status["contract_satisfied"] is True
    assert status["evidence"]["passed"] == list(range(1, 21))


def test_open_mode_is_labelled_unconfined():
    status = wp.policy_status("nope", config={})
    assert status["contract_satisfied"] is False
    assert status["configured_ready"] is False
    assert "UNCONFINED" in status["note"]


def test_a_malformed_policy_is_reported_not_defaulted():
    status = wp.policy_status("x", config={"kanban": {"workspace_policy": []}})
    assert status["mode"] == "malformed"
    assert status["contract_satisfied"] is False


@pytest.mark.parametrize("bad", [
    {"kanban": {"workspace_policy": []}},
    {"kanban": {"workspace_policy": {"boards": []}}},
    {"kanban": {"workspace_policy": {"boards": {"eval": "nope"}}}},
])
def test_malformed_policy_shapes_refuse(bad):
    with pytest.raises(PreflightRefusal):
        wp.resolve_policy("eval", config=bad)


# ===================== mandatory fields cannot be empty ====================


@pytest.mark.parametrize("field", [
    "allowed_roots", "protected_paths", "hermes_home_root",
    "required_deny_globs", "prohibited_commands", "allowed_commands",
])
def test_sandbox_refuses_an_empty_mandatory_field(tmp_path, field):
    """'Unspecified' must never be read as 'unrestricted'."""
    empty = None if field == "hermes_home_root" else ()
    with pytest.raises(PreflightRefusal, match=field):
        wp.assert_policy_wellformed(_policy(tmp_path, **{field: empty}))


def test_a_wellformed_sandbox_policy_passes(tmp_path):
    wp.assert_policy_wellformed(_policy(tmp_path))


def test_there_is_no_switch_to_disable_a_mandatory_check():
    """The earlier `require_*: false` relaxations are gone."""
    fields = set(wp.WorkspacePolicy.__dataclass_fields__)
    for gone in ("require_origin_less", "require_no_alternates",
                 "require_no_secrets", "require_no_untracked_source",
                 "require_single_query_deny"):
        assert gone not in fields, f"{gone} can still disable a mandatory check"


@pytest.mark.parametrize("glob", ["*git push*", "*Documents/dev *"])
def test_a_glob_with_a_literal_space_is_refused(tmp_path, glob):
    with pytest.raises(PreflightRefusal, match="literal space"):
        wp.assert_policy_wellformed(_policy(tmp_path, protected_paths=(glob,)))


# ===================== inability to inspect is refusal =====================


def test_a_non_git_directory_is_refused(tmp_path):
    """It previously passed a policy demanding an origin-less git fixture."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(PreflightRefusal, match="not a git repository"):
        wp._git_dir(str(plain))


def test_an_unreadable_git_indirection_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").write_text("gitdir: /nonexistent/elsewhere")
    with pytest.raises(PreflightRefusal, match="does not exist"):
        wp._git_dir(str(ws))


def test_a_malformed_git_file_is_refused(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / ".git").write_text("not an indirection")
    with pytest.raises(PreflightRefusal, match="not a gitdir indirection"):
        wp._git_dir(str(ws))


def test_a_failing_git_command_is_refused(tmp_path):
    """A nonzero return code must not read as 'clean'."""
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(PreflightRefusal, match="failed"):
        wp._git(str(plain), "remote")


def test_a_missing_git_binary_is_refused(tmp_path, monkeypatch):
    def _no_git(*a, **k):
        raise FileNotFoundError("git")
    monkeypatch.setattr(subprocess, "run", _no_git)
    with pytest.raises(PreflightRefusal, match="git is not installed"):
        wp._git(str(tmp_path), "remote")


def test_a_timed_out_git_command_is_refused(tmp_path, monkeypatch):
    def _timeout(*a, **k):
        raise subprocess.TimeoutExpired(cmd="git", timeout=30)
    monkeypatch.setattr(subprocess, "run", _timeout)
    with pytest.raises(PreflightRefusal, match="timed out"):
        wp._git(str(tmp_path), "remote")


# ===================== fixture state =======================================


def test_a_fixture_with_a_remote_is_refused(tmp_path):
    fx = _fixture(tmp_path / "fx", remote="https://example.invalid/r.git")
    with pytest.raises(PreflightRefusal, match="origin-less"):
        wp.assert_no_remotes(str(fx))


def test_an_origin_less_fixture_passes(tmp_path):
    wp.assert_no_remotes(str(_fixture(tmp_path / "fx")))


def test_alternates_are_refused(tmp_path):
    fx = _fixture(tmp_path / "fx", alternates=True)
    with pytest.raises(PreflightRefusal, match="alternate"):
        wp.assert_no_alternates(str(fx))


def test_untracked_source_is_refused(tmp_path):
    fx = _fixture(tmp_path / "fx", untracked=True)
    with pytest.raises(PreflightRefusal, match="untracked"):
        wp.assert_no_untracked_source(str(fx))


@pytest.mark.parametrize("name", [".env", ".env.production", ".netrc"])
def test_secret_files_are_refused(tmp_path, name):
    fx = _fixture(tmp_path / "fx")
    (fx / name).write_text("TOKEN=redacted")
    with pytest.raises(PreflightRefusal, match=name):
        wp.assert_no_secret_files(str(fx))


@pytest.mark.parametrize("name", [".vercel", ".aws", ".ssh"])
def test_secret_directories_are_refused(tmp_path, name):
    fx = _fixture(tmp_path / "fx")
    (fx / name).mkdir()
    with pytest.raises(PreflightRefusal, match=name):
        wp.assert_no_secret_files(str(fx))


# ===================== fixture-build attestation ===========================


def test_an_attestation_verifies_immediately_after_build(tmp_path):
    fx = _fixture(tmp_path / "fx")
    wp.verify_fixture_attestation("t", str(fx))


def test_a_fixture_without_an_attestation_is_refused(tmp_path):
    fx = _fixture(tmp_path / "fx", attest=False)
    with pytest.raises(PreflightRefusal, match="no build attestation"):
        wp.verify_fixture_attestation("t", str(fx))


def test_a_new_commit_invalidates_the_attestation(tmp_path):
    """Drift from the audited build is what this detects."""
    fx = _fixture(tmp_path / "fx")
    (fx / "new.txt").write_text("added later")
    subprocess.run(["git", "-C", str(fx), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(fx), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "drift"],
                   check=True, capture_output=True)
    with pytest.raises(PreflightRefusal, match="drifted"):
        wp.verify_fixture_attestation("t", str(fx))


def test_an_added_remote_invalidates_the_attestation(tmp_path):
    fx = _fixture(tmp_path / "fx")
    subprocess.run(["git", "-C", str(fx), "remote", "add", "origin",
                    "https://example.invalid/r.git"], check=True,
                   capture_output=True)
    with pytest.raises(PreflightRefusal, match="drifted"):
        wp.verify_fixture_attestation("t", str(fx))


def test_a_corrupt_attestation_is_refused(tmp_path):
    fx = _fixture(tmp_path / "fx")
    (fx / wp.ATTESTATION_FILENAME).write_text("{not json")
    with pytest.raises(PreflightRefusal, match="could not be read"):
        wp.verify_fixture_attestation("t", str(fx))


def test_a_wrong_version_attestation_is_refused(tmp_path):
    fx = _fixture(tmp_path / "fx")
    data = json.loads((fx / wp.ATTESTATION_FILENAME).read_text())
    data["version"] = 999
    (fx / wp.ATTESTATION_FILENAME).write_text(json.dumps(data))
    with pytest.raises(PreflightRefusal, match="version"):
        wp.verify_fixture_attestation("t", str(fx))


def test_the_attestation_is_integrity_not_security(tmp_path):
    """Documented honestly: same-user editable, so it proves drift, not intent.

    Re-running the builder after tampering produces a valid attestation again.
    That is the limit, and the docstring says so.
    """
    fx = _fixture(tmp_path / "fx")
    (fx / "tampered.txt").write_text("x")
    subprocess.run(["git", "-C", str(fx), "add", "-A"], check=True,
                   capture_output=True)
    subprocess.run(["git", "-C", str(fx), "-c", "user.email=t@t",
                    "-c", "user.name=t", "commit", "-qm", "t"], check=True,
                   capture_output=True)
    wp.build_fixture_attestation(str(fx))
    wp.verify_fixture_attestation("t", str(fx))
    assert "INTEGRITY, NOT SECURITY" in wp.build_fixture_attestation.__doc__


# ===================== allowed-root identity ===============================


def test_allowed_roots_are_pinned_by_identity(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    pinned = wp.pin_allowed_roots(_policy(root))
    st = os.stat(root)
    assert pinned[str(root)] == (st.st_dev, st.st_ino)


def test_a_missing_allowed_root_is_refused(tmp_path):
    with pytest.raises(PreflightRefusal, match="does not exist"):
        wp.pin_allowed_roots(_policy(tmp_path / "absent"))


def test_a_retargeted_allowed_root_is_caught(tmp_path):
    """The reviewer's reproduction: the root symlink moved after evaluation."""
    good = tmp_path / "good"
    evil = tmp_path / "evil"
    good.mkdir()
    evil.mkdir()
    link = tmp_path / "root"
    try:
        link.symlink_to(good)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")

    policy = _policy(link)
    pinned = wp.pin_allowed_roots(policy)
    ws = good / "ws"
    ws.mkdir()

    link.unlink()
    link.symlink_to(evil)
    with pytest.raises(PreflightRefusal, match="changed identity"):
        wp.revalidate_allowed_roots("t", str(ws), policy, pinned)


def test_a_workspace_outside_every_root_is_refused_at_revalidation(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    policy = _policy(root)
    with pytest.raises(PreflightRefusal, match="not inside any authorized root"):
        wp.revalidate_allowed_roots("t", str(outside), policy,
                                    wp.pin_allowed_roots(policy))


# ===================== credentials =========================================


def test_the_scanner_never_returns_a_secret_value(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    secret = "sk-or-EXAMPLENOTREAL1111111111"
    (home / "creds.json").write_text(f'{{"key": "{secret}"}}')
    findings = wp.scan_for_key_material(str(home))
    assert findings
    assert all(secret not in f for f in findings)


def test_key_material_refuses_without_exposing_the_value(tmp_path):
    home = tmp_path / "h"
    home.mkdir()
    secret = "sk-ant-EXAMPLENOTREAL0000000000"
    (home / "config.yaml").write_text(f"api_key: {secret}\n")
    with pytest.raises(PreflightRefusal) as exc:
        wp.assert_no_live_key("t", _policy(tmp_path), home=str(home))
    assert secret not in str(exc.value)
    assert "config.yaml" in str(exc.value)


def test_an_unreadable_eligible_file_fails_closed(tmp_path):
    """Inability to inspect is not evidence of cleanliness."""
    home = tmp_path / "h"
    home.mkdir()
    blocked = home / "creds.json"
    blocked.write_text("x")
    os.chmod(blocked, 0o000)
    try:
        findings = wp.scan_for_key_material(str(home), strict=True)
        if not findings:
            pytest.skip("running as a user that can read mode-000 files")
        assert any("unreadable" in f for f in findings)
    finally:
        os.chmod(blocked, 0o600)


def test_a_forbidden_digest_matches_without_revealing_the_key(tmp_path):
    import hashlib
    home = tmp_path / "h"
    home.mkdir()
    blob = b"opaque-bytes-with-no-known-prefix"
    (home / "blob.bin").write_bytes(blob)
    digest = hashlib.sha256(blob).hexdigest()
    assert wp.scan_for_key_material(str(home)) == []
    findings = wp.scan_for_key_material(str(home), forbidden_sha256=(digest,))
    assert findings and "forbidden key digest" in findings[0]


def test_a_hermes_home_outside_the_sandbox_is_refused(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(PreflightRefusal, match="outside the sandbox root"):
        wp.assert_hermes_home("t", _policy(root), home=str(outside))


def test_an_unset_hermes_home_root_is_refused(tmp_path):
    """It previously disabled confinement while still reporting confined."""
    home = tmp_path / "h"
    home.mkdir()
    with pytest.raises(PreflightRefusal, match="outside the sandbox root"):
        wp.assert_hermes_home("t", _policy(tmp_path, hermes_home_root=None),
                              home=str(home))


# ===================== the shipped guards ==================================


def test_assertions_18_19_call_hermes_own_guard(guards):
    assert wp.shipped_guard_blocks(wp.GUARD_PROBE_PUSH) is True
    assert wp.shipped_guard_blocks(wp.GUARD_PROBE_VERCEL) is True
    assert wp.shipped_guard_blocks("npm test") is False


def test_assertion_20_calls_hermes_own_execute_code_guard(guards):
    assert wp.shipped_guard_refuses_execute_code(wp.GUARD_PROBE_CODE) is True


def test_the_execute_code_probe_restores_the_session_env(guards):
    key = "HERMES_SINGLE_QUERY_SESSION"
    before = os.environ.get(key)
    wp.shipped_guard_refuses_execute_code(wp.GUARD_PROBE_CODE)
    assert os.environ.get(key) == before


def test_a_config_that_does_not_block_pushes_fails_the_matrix(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda *a, **k: {"approvals": {"single_query_mode": "deny"}})
    with pytest.raises(PreflightRefusal, match="does NOT block"):
        wp.assert_prohibited_matrix("t", _policy(tmp_path))


def test_an_overbroad_deny_blocking_real_work_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda *a, **k: {"approvals": {"single_query_mode": "deny",
                                       "deny": ["*git*", "*npm*"]}})
    with pytest.raises(PreflightRefusal, match="legitimate commands"):
        wp.assert_allowed_matrix("t", _policy(tmp_path))


def test_single_query_mode_must_be_deny(guards, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda *a, **k: {"approvals": {"single_query_mode": "allow"}})
    with pytest.raises(PreflightRefusal, match="single_query_mode"):
        wp.assert_single_query_deny("t")


def test_missing_required_deny_globs_refuse(tmp_path, monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly",
                        lambda *a, **k: {"approvals": {"deny": ["*vercel*"]}})
    with pytest.raises(PreflightRefusal, match="missing required globs"):
        wp.assert_required_deny_globs("t", _policy(tmp_path))


# ===================== the full set, end to end ============================


def test_a_fully_compliant_sandbox_board_passes_all_twenty(tmp_path, guards,
                                                           monkeypatch):
    root = tmp_path / "sandbox"
    root.mkdir()
    fx = _fixture(root / "fixtures" / "app")
    home = root / "hermes-home"
    home.mkdir()
    (home / "settings.yaml").write_text("kanban:\n  max_in_progress: 1\n")

    policy = _policy(root, hermes_home_root=str(root))
    report = wp.enforce_final(
        "t", str(fx), policy=policy, launch=_Launch(fx),
        pinned_roots=wp.pin_allowed_roots(policy), home=str(home),
    )
    assert report.contract_satisfied is True, report.summary()
    assert report.passed == wp.REQUIRED_ASSERTION_IDS
    assert not report.skipped and not report.failed and not report.missing


def test_an_unverified_launch_fails_assertion_one(tmp_path, guards):
    root = tmp_path / "sandbox"
    root.mkdir()
    fx = _fixture(root / "fixtures" / "app")
    home = root / "hermes-home"
    home.mkdir()
    policy = _policy(root, hermes_home_root=str(root))
    with pytest.raises(PreflightRefusal, match="not independently verified"):
        wp.enforce_final("t", str(fx), policy=policy,
                         launch=_Launch(fx, status="unobservable"),
                         home=str(home))


def test_a_verified_launch_elsewhere_fails_assertion_one(tmp_path, guards):
    root = tmp_path / "sandbox"
    root.mkdir()
    fx = _fixture(root / "fixtures" / "app")
    other = root / "other"
    other.mkdir()
    home = root / "hermes-home"
    home.mkdir()
    policy = _policy(root, hermes_home_root=str(root))
    with pytest.raises(PreflightRefusal, match="is not the final workspace"):
        wp.enforce_final("t", str(fx), policy=policy, launch=_Launch(other),
                         home=str(home))


def test_open_mode_final_enforcement_skips_everything_and_claims_nothing(tmp_path):
    report = wp.enforce_final("t", str(tmp_path),
                              policy=_policy(tmp_path, mode=wp.MODE_OPEN),
                              launch=_Launch(tmp_path))
    assert report.contract_satisfied is False
    assert report.skipped == wp.REQUIRED_ASSERTION_IDS


def test_the_pre_claim_gate_never_claims_the_contract(tmp_path):
    """It judges a declared destination; the artifact does not exist yet."""
    ws = tmp_path / "ws"
    ws.mkdir()
    report = wp.enforce("t", str(ws), policy=_policy(tmp_path))
    assert report.contract_satisfied is False
    assert report.skipped, "pre-claim must record what it deferred"
