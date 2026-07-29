from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
RUNTIME = ROOT / "ops" / "muncho" / "runtime"
RELEASE_ENTRYPOINTS = ("hermes", "hermes-acp", "hermes-agent", "muncho-ops")


def _load_routine():
    sys.path.insert(0, str(RUNTIME))
    try:
        spec = importlib.util.spec_from_file_location(
            "fork_upstream_auto_sync_pr_routine",
            RUNTIME / "fork_upstream_auto_sync_pr_routine.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(RUNTIME))


def test_runtime_stale_detection_checks_current_upstream_ancestry(monkeypatch):
    routine = _load_routine()
    snapshot = "1" * 40
    current = "2" * 40
    fork = "3" * 40
    head = "4" * 40
    pr = {
        "headRefName": "codex/upstream-sync-auto-20260711-1200",
        "headRefOid": head,
        "body": f"Automated fork-only upstream sync PR\nUpstream main: `{snapshot}`",
    }
    fresh = {
        "fork_main_ref": fork,
        "merge_base": "5" * 40,
        "upstream_main_ref": current,
    }

    monkeypatch.setattr(routine, "is_auto_owned_sync_pr", lambda _: True)

    def contains(repo, base, candidate):
        return repo == routine.UPSTREAM_REPO and base == snapshot and candidate == current

    monkeypatch.setattr(routine, "compare_shows_head_contains_base", contains)
    assert routine.stale_sync_reason(pr, fresh) == "upstream_snapshot_superseded"


def test_runtime_dedupe_suppresses_unchanged_blocker(tmp_path, monkeypatch):
    routine = _load_routine()
    monkeypatch.setattr(routine, "BLOCKER_DEDUPE_STATE", tmp_path / "dedupe.json")
    report = {
        "status": "blocked_auto_merge_deploy_gate",
        "auto_merge_deploy": {
            "blockers": ["checks_failed", "merge_state_UNSTABLE"],
            "checks": {
                "failure_like_checks": [
                    {"name": "Python tests / slice 5", "conclusion": "FAILURE"}
                ]
            },
        },
    }
    pr = {"number": 91, "headRefOid": "6" * 40}

    assert routine.apply_blocker_notification_dedupe(report, pr) is True
    assert routine.apply_blocker_notification_dedupe(report, pr) is False
    assert (
        report["blocker_notification"]["reason"]
        == "unchanged_selection_suppressed_unconfirmed"
    )
    assert report["blocker_notification"]["delivery_confirmed_at"] is None


def test_runtime_dedupe_treats_merge_conflict_paths_as_stable_identity(
    tmp_path, monkeypatch
):
    routine = _load_routine()
    monkeypatch.setattr(routine, "BLOCKER_DEDUPE_STATE", tmp_path / "dedupe.json")
    report = {
        "status": "blocked_merge_conflicts",
        "fresh_refs": {
            "fork_main_ref": "a" * 40,
            "upstream_main_ref": "1" * 40,
            "behind_by": 196,
        },
        "conflicted_files": ["gateway/run.py", "tools/approval.py"],
    }

    assert routine.apply_blocker_notification_dedupe(report, {}) is True

    # Upstream movement is evidence for the report, not a new blocker. The
    # same conflict set stays suppressed until the 24-hour reminder window.
    report["fresh_refs"] = {
        "fork_main_ref": "a" * 40,
        "upstream_main_ref": "2" * 40,
        "behind_by": 211,
    }
    assert routine.apply_blocker_notification_dedupe(report, {}) is False

    # A materially different conflict set is a new blocker and emits now.
    report["conflicted_files"].append("hermes_cli/config.py")
    assert routine.apply_blocker_notification_dedupe(report, {}) is True

    # A new fork base can change the conflict itself even when path names stay
    # the same, so it is a new blocker identity and must notify immediately.
    assert routine.apply_blocker_notification_dedupe(report, {}) is False
    report["fresh_refs"]["fork_main_ref"] = "b" * 40
    assert routine.apply_blocker_notification_dedupe(report, {}) is True


def test_deploy_marks_planned_stop_before_symlink_swap_and_restart():
    source = (RUNTIME / "muncho-auto-deploy-release").read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]
    marker = run_deploy.index('marker_output="$(')
    symlink_swap = run_deploy.index('ln -sfn "$new" "$ACTIVE_LINK.next"')
    restart = run_deploy.index('systemctl restart "$SERVICE"')
    verify_consumed = run_deploy.index('planned_stop_marker_not_consumed')

    assert marker < symlink_swap < restart < verify_consumed
    assert 'blocked_planned_restart_helper_missing' in source
    assert 'blocked_planned_stop_marker_failed' in source
    assert 'rollback_release() {' in source
    assert 'ln -sfn "$previous" "$ACTIVE_LINK.rollback"' in source
    assert 'write_status "deploy_rolled_back"' in source
    assert 'REPO_URL="https://github.com/lomliev/hermes-agent.git"' in source
    assert "MUNCHO_REPO_URL" not in source
    assert 'release_identity_matches "$active" "$active_head"' in source
    assert 'release_identity_matches "$new" "$sha"' in source
    assert '"$RELEASES/hermes-agent-${expected_head:0:12}"' in source
    assert 'DEPLOY_HEALTH_WAIT_SECONDS" -gt 300' in source
    assert "previous_release_identity_invalid" in source
    assert '"restored_source":' not in source


def test_deploy_staging_dependency_package_is_final_address_bound():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]
    prepare = run_deploy.index(
        'package_production_runtime_dependencies.py" prepare'
    )
    prepare_address = run_deploy.index(
        '--release-address "$new"',
        prepare,
    )
    prepare_revision = run_deploy.index('--revision "$sha"', prepare_address)
    seal = run_deploy.index(
        'seal_agent_browser_config "$tmp" "$sha"',
        prepare_revision,
    )
    build = run_deploy.index(
        'package_production_runtime_dependencies.py" build-manifest',
        seal,
    )
    verify = run_deploy.index(
        'package_production_runtime_dependencies.py" verify',
        build,
    )
    verify_address = run_deploy.index(
        '--release-address "$new"',
        verify,
    )
    move = run_deploy.index('mv -T "$tmp" "$new"', verify_address)

    assert (
        prepare
        < prepare_address
        < prepare_revision
        < seal
        < build
        < verify
        < verify_address
        < move
    )
    syntax = subprocess.run(
        ["bash", "-n", str(helper)],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert syntax.returncode == 0, syntax.stderr


def test_deploy_reinstalls_and_attests_entrypoints_at_final_address():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]

    publish = run_deploy.index('mv -T "$tmp" "$new"')
    identity = run_deploy.index('release_identity_matches "$new" "$sha"')
    final_install = run_deploy.index(
        'install_target_release_wheel "$new" "$new"',
        identity,
    )
    entrypoint_attest = run_deploy.index(
        'attest_target_release_entrypoints "$new"',
        final_install,
    )
    venv_attest = run_deploy.index(
        'attest_target_release_venv "$new" "$new"',
        entrypoint_attest,
    )
    cutover_attest = run_deploy.index(
        'cutover_artifacts_match "$new" "$sha"',
        venv_attest,
    )
    activate = run_deploy.index('ln -sfn "$new" "$ACTIVE_LINK.next"')

    assert (
        publish
        < identity
        < final_install
        < entrypoint_attest
        < venv_attest
        < cutover_attest
        < activate
    )
    assert '\\"stage\\": \\"final_address_wheel_install\\"' in run_deploy
    assert "blocked_target_release_entrypoints_invalid" in run_deploy


def test_deploy_lock_and_active_release_rechecks_precede_target_mutations():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]
    lock_function = source[
        source.index("acquire_deploy_lock_at() {") : source.index(
            "gateway_deploy_topology_json() {"
        )
    ]

    lock = run_deploy.index('acquire_deploy_lock "$sha" "$pr"')
    pre_lock_topology = run_deploy.index(
        'require_legacy_deploy_topology "$sha" "$pr" "pre_deploy"'
    )
    post_lock_topology = run_deploy.index(
        'require_legacy_deploy_topology "$sha" "$pr" "post_deploy_lock"'
    )
    active_snapshot = run_deploy.index(
        'active="$(readlink -f "$ACTIVE_LINK" 2>/dev/null || true)"'
    )
    publish_guard = run_deploy.index('"pre_release_publish"')
    publish = run_deploy.index('mv -T "$tmp" "$new"', publish_guard)
    final_install_guard = run_deploy.index('"pre_final_address_wheel_install"')
    final_install = run_deploy.index(
        'install_target_release_wheel "$new" "$new"',
        final_install_guard,
    )
    restart_marker_guard = run_deploy.index('"pre_restart_marker"')
    restart_marker = run_deploy.index('marker_output="$(', restart_marker_guard)
    activation_guard = run_deploy.index('"pre_link_activation"')
    activation = run_deploy.index(
        'ln -sfn "$new" "$ACTIVE_LINK.next"',
        activation_guard,
    )

    assert pre_lock_topology < lock < post_lock_topology < active_snapshot
    assert publish_guard < publish
    assert final_install_guard < final_install
    assert restart_marker_guard < restart_marker
    assert activation_guard < activation
    assert 'DEPLOY_LOCK_PATH="/run/muncho-auto-deploy-release.lock"' in source
    assert 'SYSTEM_FLOCK="/usr/bin/flock"' in source
    assert 'exec 9<>"$lock_path"' in lock_function
    assert '"$SYSTEM_FLOCK" --exclusive --nonblock 9' in lock_function
    assert "blocked_concurrent_deploy" in lock_function


def test_already_active_fast_path_is_read_only_and_fully_attested():
    helper = RUNTIME / "muncho-auto-deploy-release"
    source = helper.read_text(encoding="utf-8")
    run_deploy = source[source.index("run_deploy() {") : source.index("main() {")]
    fast_path = run_deploy[
        run_deploy.index('if [ "$active" = "$new" ]; then') : run_deploy.index(
            'require_no_active_voice_call "$sha" "$pr" "pre_release"'
        )
    ]
    venv_attestation = source[
        source.index("attest_target_release_venv() {") : source.index(
            "install_target_release_wheel() {"
        )
    ]
    cutover_attestation = source[
        source.index("cutover_artifacts_match() {") : source.index(
            "cleanup_old_releases() {"
        )
    ]

    entrypoint = fast_path.index('attest_target_release_entrypoints "$active"')
    venv = fast_path.index('attest_target_release_venv "$active" "$active"')
    cutover = fast_path.index('cutover_artifacts_match "$active" "$sha"')
    deploy_pass = fast_path.index('write_status "deploy_pass"')
    completed = fast_path.index("return 0", deploy_pass)

    assert entrypoint < venv < cutover < deploy_pass < completed
    assert "install_target_release_wheel" not in fast_path
    assert " pip " not in fast_path
    assert "ln -sfn" not in fast_path
    assert "mv -T" not in fast_path
    assert "systemctl restart" not in fast_path
    assert '"already_active\\": true' in fast_path
    assert '"$release/.venv/bin/python" -I -B -P -s -' in venv_attestation
    assert cutover_attestation.count('"$release/.venv/bin/python" -I -B') == 2


def _write_fake_flock(path: Path) -> None:
    path.write_text(
        f"""\
#!{Path(sys.executable).resolve()}
import fcntl
import sys

descriptor = int(sys.argv[-1])
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
except BlockingIOError:
    raise SystemExit(1)
""",
        encoding="utf-8",
    )
    path.chmod(0o755)


def _lock_shell_script(*, hold: bool) -> str:
    suffix = (
        'printf locked > "$LOCK_MARKER"\nIFS= read -r _'
        if hold
        else 'printf "rc=0\\n"'
    )
    return f'''
set -euo pipefail
source "$1"
SYSTEM_PYTHON="$2"
SYSTEM_FLOCK="$3"
LOCK_PATH="$4"
LOCK_PARENT="$5"
TRUSTED_UID="$6"
TRUSTED_GID="$7"
STATUS_LOG="$8"
LOCK_MARKER="$9"
write_status() {{
  printf '%s\\n' "$1" >> "$STATUS_LOG"
}}
set +e
acquire_deploy_lock_at \
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
  "203" \
  "$LOCK_PATH" \
  "$LOCK_PARENT" \
  "$TRUSTED_UID" \
  "$TRUSTED_GID"
rc=$?
set -e
if [ "$rc" -ne 0 ]; then
  printf 'rc=%s\\n' "$rc"
  exit "$rc"
fi
{suffix}
'''


def _lock_process_args(
    helper: Path,
    fake_flock: Path,
    lock_path: Path,
    lock_parent: Path,
    status_log: Path,
    marker: Path,
) -> list[str]:
    return [
        "bash",
        "-c",
        _lock_shell_script(hold=False),
        "deploy-lock",
        str(helper),
        str(Path(sys.executable).resolve()),
        str(fake_flock),
        str(lock_path),
        str(lock_parent),
        str(os.getuid()),
        str(os.getgid()),
        str(status_log),
        str(marker),
    ]


def test_deploy_lock_rejects_concurrent_process_and_allows_bounded_retry(tmp_path):
    helper = RUNTIME / "muncho-auto-deploy-release"
    root = tmp_path.resolve()
    lock_parent = root / "trusted-lock-parent"
    lock_parent.mkdir(mode=0o700)
    os.chown(lock_parent, os.getuid(), os.getgid())
    lock_path = lock_parent / "deploy.lock"
    fake_flock = root / "fake-flock"
    _write_fake_flock(fake_flock)
    holder_status = root / "holder-status"
    contender_status = root / "contender-status"
    retry_status = root / "retry-status"
    marker = root / "lock-held"
    holder_args = _lock_process_args(
        helper,
        fake_flock,
        lock_path,
        lock_parent,
        holder_status,
        marker,
    )
    holder_args[2] = _lock_shell_script(hold=True)
    holder = subprocess.Popen(
        holder_args,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and holder.poll() is None:
            if time.monotonic() >= deadline:
                pytest.fail("timed out waiting for the first deploy lock holder")
            time.sleep(0.02)
        assert holder.poll() is None, holder.stderr.read() if holder.stderr else ""

        contender = subprocess.run(
            _lock_process_args(
                helper,
                fake_flock,
                lock_path,
                lock_parent,
                contender_status,
                root / "unused-contender-marker",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert contender.returncode == 12
        assert "rc=12" in contender.stdout
        assert contender_status.read_text(encoding="utf-8").splitlines() == [
            "blocked_concurrent_deploy"
        ]

        assert holder.stdin is not None
        holder.stdin.write("release\n")
        holder.stdin.flush()
        holder_stdout, holder_stderr = holder.communicate(timeout=10)
        assert holder.returncode == 0, holder_stderr
        assert holder_stdout == ""

        retry = subprocess.run(
            _lock_process_args(
                helper,
                fake_flock,
                lock_path,
                lock_parent,
                retry_status,
                root / "unused-retry-marker",
            ),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert retry.returncode == 0, retry.stderr
        assert retry.stdout.strip() == "rc=0"
        assert not retry_status.exists()
    finally:
        if holder.poll() is None:
            holder.terminate()
            holder.wait(timeout=10)


@pytest.mark.parametrize("artifact_kind", ["symlink", "hardlink", "wrong-mode"])
def test_deploy_lock_rejects_untrusted_lock_file_artifact(tmp_path, artifact_kind):
    helper = RUNTIME / "muncho-auto-deploy-release"
    root = tmp_path.resolve()
    lock_parent = root / "trusted-lock-parent"
    lock_parent.mkdir(mode=0o700)
    os.chown(lock_parent, os.getuid(), os.getgid())
    lock_path = lock_parent / "deploy.lock"
    external = root / f"{artifact_kind}-source"
    external.write_text("untrusted lock artifact\n", encoding="utf-8")
    external.chmod(0o600)
    os.chown(external, os.getuid(), os.getgid())
    if artifact_kind == "symlink":
        lock_path.symlink_to(external)
    elif artifact_kind == "hardlink":
        os.link(external, lock_path)
    else:
        lock_path.write_text("wrong mode\n", encoding="utf-8")
        lock_path.chmod(0o644)
        os.chown(lock_path, os.getuid(), os.getgid())
    fake_flock = root / "fake-flock"
    _write_fake_flock(fake_flock)
    status_log = root / "status"

    rejected = subprocess.run(
        _lock_process_args(
            helper,
            fake_flock,
            lock_path,
            lock_parent,
            status_log,
            root / "unused-marker",
        ),
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert rejected.returncode == 12
    assert "rc=12" in rejected.stdout
    assert status_log.read_text(encoding="utf-8").splitlines() == [
        "blocked_deploy_lock_invalid"
    ]


def _run_entrypoint_attestation(helper: Path, release: Path) -> subprocess.CompletedProcess:
    owner = subprocess.run(
        ["id", "-un"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    script = r'''
set -euo pipefail
source "$1"
OWNER="$2"
release="$3"
sudo() {
  if [ "$1" = "-n" ] && [ "$2" = "-u" ]; then
    shift 3
  fi
  command "$@"
}
attest_target_release_entrypoints "$release"
'''
    return subprocess.run(
        ["bash", "-c", script, "entrypoint-attest", str(helper), owner, str(release)],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )


def _write_valid_entrypoint_fixture(release: Path) -> Path:
    release = release.resolve()
    bin_dir = release / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(Path(sys.executable).resolve())
    expected_shebang = f"#!{bin_dir / 'python'}\n"
    for name in RELEASE_ENTRYPOINTS:
        path = bin_dir / name
        path.write_text(
            expected_shebang + "print('entrypoint-fixture-ok')\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    return release


def test_release_entrypoint_attestation_rejects_staging_shebang_after_rename(
    tmp_path,
):
    helper = RUNTIME / "muncho-auto-deploy-release"
    staging = tmp_path / ".hermes-agent-deadbeef0000.tmp.123"
    release = tmp_path / "hermes-agent-deadbeef0000"
    subprocess.run(
        [sys.executable, "-m", "venv", str(staging / ".venv")],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    bin_dir = staging / ".venv" / "bin"
    stale_shebang = f"#!{bin_dir / 'python'}\n"
    for name in RELEASE_ENTRYPOINTS:
        path = bin_dir / name
        path.write_text(stale_shebang + "raise SystemExit(0)\n", encoding="utf-8")
        path.chmod(0o755)
    staging.rename(release)

    rejected = _run_entrypoint_attestation(helper, release)

    assert rejected.returncode != 0
    assert "BLOCKED_RELEASE_ENTRYPOINT_INVALID:hermes" in rejected.stderr

    final_shebang = f"#!{release / '.venv/bin/python'}\n"
    for name in RELEASE_ENTRYPOINTS:
        path = release / ".venv" / "bin" / name
        path.write_text(final_shebang + "raise SystemExit(0)\n", encoding="utf-8")
        path.chmod(0o755)

    accepted = _run_entrypoint_attestation(helper, release)

    assert accepted.returncode == 0, accepted.stderr
    assert accepted.stdout.strip() == "target_release_entrypoints=ok"


def test_release_entrypoint_attestation_rejects_symlinked_bin_without_execution(
    tmp_path,
):
    helper = RUNTIME / "muncho-auto-deploy-release"
    root = tmp_path.resolve()
    release = root / "hermes-agent-deadbeef0000"
    external_bin = root / "external-bin"
    marker = root / "external-python-invoked"
    external_bin.mkdir()
    external_python = external_bin / "python"
    external_python.write_text(
        "#!/bin/sh\n"
        f"printf invoked > {str(marker)!r}\n"
        "exit 99\n",
        encoding="utf-8",
    )
    external_python.chmod(0o755)
    for name in RELEASE_ENTRYPOINTS:
        path = external_bin / name
        path.write_text(
            f"#!{external_python}\nraise SystemExit(0)\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
    (release / ".venv").mkdir(parents=True)
    (release / ".venv" / "bin").symlink_to(external_bin)

    rejected = _run_entrypoint_attestation(helper, release)

    assert rejected.returncode != 0
    assert "BLOCKED_RELEASE_ENTRYPOINT_BIN_INVALID" in rejected.stderr
    assert not marker.exists()


@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_release_entrypoint_attestation_rejects_linked_launcher(
    tmp_path,
    link_kind,
):
    helper = RUNTIME / "muncho-auto-deploy-release"
    release = _write_valid_entrypoint_fixture(
        tmp_path.resolve() / f"hermes-agent-{link_kind}"
    )
    launcher = release / ".venv" / "bin" / "hermes"
    linked_path = tmp_path.resolve() / f"{link_kind}-target"

    if link_kind == "symlink":
        linked_path.write_bytes(launcher.read_bytes())
        linked_path.chmod(0o755)
        launcher.unlink()
        launcher.symlink_to(linked_path)
    else:
        os.link(launcher, linked_path)

    rejected = _run_entrypoint_attestation(helper, release)

    assert rejected.returncode != 0
    assert "BLOCKED_RELEASE_ENTRYPOINT_" in rejected.stderr
    assert "hermes" in rejected.stderr


def test_entrypoint_attestation_anchors_directory_and_open_launcher_identity():
    source = (RUNTIME / "muncho-auto-deploy-release").read_text(encoding="utf-8")
    attestation = source[
        source.index("attest_target_release_entrypoints() {") : source.index(
            "release_identity_matches() {"
        )
    ]

    assert 'getattr(os, "O_DIRECTORY", 0)' in attestation
    assert attestation.count('getattr(os, "O_NOFOLLOW", 0)') >= 2
    assert "bin_descriptor = os.open(bin_path, dir_flags)" in attestation
    assert (
        "before = os.stat(name, dir_fd=bin_descriptor, follow_symlinks=False)"
        in attestation
    )
    assert "fd = os.open(name, flags, dir_fd=bin_descriptor)" in attestation
    assert "opened_before = os.fstat(fd)" in attestation
    assert "opened_after = os.fstat(fd)" in attestation
    assert "anchored_after = os.stat(" in attestation
    assert "identity(before) != identity(opened_before)" in attestation
    assert "identity(before) != identity(opened_after)" in attestation
    assert "identity(before) != identity(anchored_after)" in attestation
    assert "os.close(fd)" in attestation


def _pip_install_probe_package(python: Path, source: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            str(python),
            "-m",
            "pip",
            "--isolated",
            "install",
            "--no-index",
            "--no-deps",
            "--no-build-isolation",
            "--no-cache-dir",
            "--force-reinstall",
            str(source),
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_real_pip_reinstall_rebinds_all_entrypoints_after_staging_rename():
    if importlib.util.find_spec("setuptools") is None:
        pytest.skip("setuptools is required for the local no-network package build")

    helper = RUNTIME / "muncho-auto-deploy-release"
    with tempfile.TemporaryDirectory(
        prefix="muncho-entrypoints-",
        dir="/tmp",
    ) as raw_root:
        root = Path(raw_root).resolve()
        staging = root / ".hermes-agent-deadbeef0000.tmp.123"
        release = root / "hermes-agent-deadbeef0000"
        staging.mkdir()
        (staging / "pyproject.toml").write_text(
            """\
[build-system]
requires = []
build-backend = "setuptools.build_meta"

[project]
name = "muncho-entrypoint-rebind-probe"
version = "0.0.1"

[project.scripts]
hermes = "entrypoint_probe:main"
hermes-acp = "entrypoint_probe:main"
hermes-agent = "entrypoint_probe:main"
muncho-ops = "entrypoint_probe:main"

[tool.setuptools]
py-modules = ["entrypoint_probe"]
""",
            encoding="utf-8",
        )
        (staging / "entrypoint_probe.py").write_text(
            "def main():\n"
            "    print('entrypoint-probe-ok')\n",
            encoding="utf-8",
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "venv",
                "--system-site-packages",
                str(staging / ".venv"),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        staging_python = staging / ".venv" / "bin" / "python"
        first_install = _pip_install_probe_package(staging_python, staging)
        assert first_install.returncode == 0, first_install.stderr
        os.chown(staging / ".venv" / "bin", os.getuid(), os.getgid())
        for name in RELEASE_ENTRYPOINTS:
            os.chown(
                staging / ".venv" / "bin" / name,
                os.getuid(),
                os.getgid(),
            )
            assert (staging / ".venv" / "bin" / name).read_bytes().splitlines()[0] == (
                f"#!{staging_python}".encode("ascii")
            )

        staging.rename(release)
        stale = _run_entrypoint_attestation(helper, release)
        assert stale.returncode != 0
        assert "BLOCKED_RELEASE_ENTRYPOINT_INVALID:hermes" in stale.stderr

        release_python = release / ".venv" / "bin" / "python"
        final_install = _pip_install_probe_package(release_python, release)
        assert final_install.returncode == 0, final_install.stderr
        os.chown(release / ".venv" / "bin", os.getuid(), os.getgid())
        for name in RELEASE_ENTRYPOINTS:
            os.chown(
                release / ".venv" / "bin" / name,
                os.getuid(),
                os.getgid(),
            )
        accepted = _run_entrypoint_attestation(helper, release)
        assert accepted.returncode == 0, accepted.stderr
        assert accepted.stdout.strip() == "target_release_entrypoints=ok"
        for name in RELEASE_ENTRYPOINTS:
            launcher = release / ".venv" / "bin" / name
            assert launcher.read_bytes().splitlines()[0] == (
                f"#!{release_python}".encode("ascii")
            )
            executed = subprocess.run(
                [str(launcher)],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert executed.returncode == 0, executed.stderr
            assert executed.stdout.strip() == "entrypoint-probe-ok"


def _run_active_invariance_check(
    helper: Path,
    active_link: Path,
    expected_active: Path,
    target: Path,
    stage: str,
) -> subprocess.CompletedProcess:
    script = r'''
set -euo pipefail
source "$1"
ACTIVE_LINK="$2"
write_status() {
  printf 'status=%s extra=%s\n' "$1" "$4"
}
set +e
require_inactive_target_with_unchanged_active \
  "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa" \
  "203" \
  "$3" \
  "$4" \
  "$5"
rc=$?
set -e
printf 'rc=%s\n' "$rc"
exit "$rc"
'''
    return subprocess.run(
        [
            "bash",
            "-c",
            script,
            "active-invariance",
            str(helper),
            str(active_link),
            str(expected_active),
            str(target),
            stage,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_active_release_invariance_guard_blocks_changed_or_active_target(tmp_path):
    helper = RUNTIME / "muncho-auto-deploy-release"
    root = tmp_path.resolve()
    expected_active = root / "hermes-agent-aaaaaaaaaaaa"
    target = root / "hermes-agent-bbbbbbbbbbbb"
    unexpected = root / "hermes-agent-cccccccccccc"
    for path in (expected_active, target, unexpected):
        path.mkdir()
    active_link = root / "active"
    active_link.symlink_to(expected_active)

    unchanged = _run_active_invariance_check(
        helper,
        active_link,
        expected_active,
        target,
        "unchanged",
    )
    assert unchanged.returncode == 0, unchanged.stderr
    assert unchanged.stdout.strip() == "rc=0"

    active_link.unlink()
    active_link.symlink_to(unexpected)
    changed = _run_active_invariance_check(
        helper,
        active_link,
        expected_active,
        target,
        "changed",
    )
    assert changed.returncode == 13
    assert "status=blocked_active_release_changed_during_deploy" in changed.stdout
    assert '"stage": "changed"' in changed.stdout
    assert '"active_unchanged": false' in changed.stdout
    assert '"target_inactive": true' in changed.stdout
    assert "rc=13" in changed.stdout

    active_link.unlink()
    active_link.symlink_to(target)
    target_active = _run_active_invariance_check(
        helper,
        active_link,
        expected_active,
        target,
        "target-active",
    )
    assert target_active.returncode == 13
    assert "status=blocked_active_release_changed_during_deploy" in target_active.stdout
    assert '"target_inactive": false' in target_active.stdout
