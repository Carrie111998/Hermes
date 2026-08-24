"""Behavioral coverage for install.sh's install-wide update lock."""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = REPO_ROOT / "scripts" / "install.sh"
HAS_FLOCK = shutil.which("flock") is not None


def _run_script(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(INSTALL_SH), *args],
        env=os.environ | {"HERMES_HOME": str(home)},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.live_system_guard_bypass
def test_install_sh_lock_claim_is_released_on_failed_stage(tmp_path: Path) -> None:
    home = tmp_path / "home"

    result = _run_script(home, "--stage", "not-a-stage", "--non-interactive")

    assert result.returncode == 2
    assert "Unknown stage" in result.stdout
    assert not (home / ".hermes-update-in-progress").exists()
    assert not list(home.glob(".hermes-update-in-progress.*.claim.*"))


@pytest.mark.live_system_guard_bypass
def test_install_sh_lock_refuses_live_foreign_owner_without_mutation(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    sleeper = subprocess.Popen(["sleep", "30"])
    marker = home / ".hermes-update-in-progress"
    payload = f"{sleeper.pid}\n1\n"
    marker.write_text(payload, encoding="utf-8")
    try:
        result = _run_script(home, "--stage", "not-a-stage", "--non-interactive")
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)

    assert result.returncode == 2
    assert "Another Hermes update owns this install" in result.stdout
    assert marker.read_text(encoding="utf-8") == payload


@pytest.mark.live_system_guard_bypass
def test_install_sh_lock_borrows_explicit_live_handoff_claim(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".hermes-update-in-progress"
    # A sibling process is live but cannot pass the ancestry fallback. Borrowing
    # therefore proves the explicit HERMES_UPDATE_HANDOFF_PID path itself.
    sibling_owner = subprocess.Popen(["sleep", "30"])
    payload = f"{sibling_owner.pid}\n1\n"
    marker.write_text(payload, encoding="utf-8")
    try:
        result = subprocess.run(
            [
                "bash",
                str(INSTALL_SH),
                "--stage",
                "not-a-stage",
                "--non-interactive",
            ],
            env=os.environ
            | {
                "HERMES_HOME": str(home),
                "HERMES_UPDATE_HANDOFF_PID": str(sibling_owner.pid),
            },
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        sibling_owner.terminate()
        sibling_owner.wait(timeout=5)

    assert result.returncode == 2
    assert "Unknown stage" in result.stdout
    assert "Another Hermes update owns" not in result.stdout
    assert marker.read_text(encoding="utf-8") == payload


@pytest.mark.live_system_guard_bypass
def test_install_sh_lock_contention_has_one_mutator(tmp_path: Path) -> None:
    home = tmp_path / "home"
    started = tmp_path / "started"
    command = (
        'installer=$1; home=$2; started=$3; set --; HERMES_HOME="$home"; '
        'source "$installer"; '
        'run_installer_with_update_lock bash -c '
        "'printf started > \"$1\"; sleep 3' _ \"$started\""
    )
    first = subprocess.Popen(
        ["bash", "-c", command, "lock-test", str(INSTALL_SH), str(home), str(started)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    deadline = time.monotonic() + 5
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    assert started.exists(), first.communicate(timeout=5)

    second = _run_script(home, "--stage", "not-a-stage", "--non-interactive")
    first_stdout, first_stderr = first.communicate(timeout=8)

    assert first.returncode == 0, first_stderr + first_stdout
    assert second.returncode == 2
    assert "Another Hermes update owns this install" in second.stdout
    assert not (home / ".hermes-update-in-progress").exists()


@pytest.mark.live_system_guard_bypass
def test_install_sh_release_never_deletes_replacement_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    marker = home / ".hermes-update-in-progress"
    replacement = "424242\n9\n"
    command = (
        'installer=$1; home=$2; marker=$3; set --; HERMES_HOME="$home"; '
        'source "$installer"; '
        'replace_claim() { tmp="${marker}.replacement"; '
        'printf "424242\\n9\\n" > "$tmp"; mv -f "$tmp" "$marker"; }; '
        'run_installer_with_update_lock replace_claim'
    )

    result = subprocess.run(
        ["bash", "-c", command, "lock-test", str(INSTALL_SH), str(home), str(marker)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert marker.read_text(encoding="utf-8") == replacement
    assert not list(home.glob(".hermes-update-in-progress.*.claim.*"))


@pytest.mark.live_system_guard_bypass
def test_install_sh_lock_fails_closed_on_malformed_marker(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    marker = home / ".hermes-update-in-progress"
    marker.write_text("truncated", encoding="utf-8")

    result = _run_script(home, "--stage", "not-a-stage", "--non-interactive")

    assert result.returncode == 2
    assert "malformed or unavailable" in result.stdout
    assert marker.read_text(encoding="utf-8") == "truncated"


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not HAS_FLOCK, reason="stale cleanup requires a shared flock primitive")
def test_install_sh_lock_retires_confirmed_dead_owner(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    former_owner = subprocess.Popen(["sleep", "30"])
    dead_pid = former_owner.pid
    former_owner.terminate()
    former_owner.wait(timeout=5)
    marker = home / ".hermes-update-in-progress"
    marker.write_text(f"{dead_pid}\n1\n", encoding="utf-8")

    result = _run_script(home, "--stage", "not-a-stage", "--non-interactive")

    assert result.returncode == 2
    assert "Unknown stage" in result.stdout
    assert "Another Hermes update owns" not in result.stdout
    assert not marker.exists()
    assert not list(home.glob(".hermes-update-in-progress.stale.snapshot.*"))


@pytest.mark.live_system_guard_bypass
def test_install_sh_dead_owner_fails_closed_when_mutex_is_unavailable(
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    former_owner = subprocess.Popen(["sleep", "30"])
    dead_pid = former_owner.pid
    former_owner.terminate()
    former_owner.wait(timeout=5)
    marker = home / ".hermes-update-in-progress"
    payload = f"{dead_pid}\n1\n"
    marker.write_text(payload, encoding="utf-8")

    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    flock_wrapper = wrapper_dir / "flock"
    flock_wrapper.write_text("#!/bin/bash\nexit 69\n", encoding="utf-8")
    flock_wrapper.chmod(0o755)

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--stage", "not-a-stage", "--non-interactive"],
        env=os.environ
        | {
            "HERMES_HOME": str(home),
            "PATH": f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}",
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Could not lock the shared update mutex" in result.stdout
    assert marker.read_text(encoding="utf-8") == payload


@pytest.mark.live_system_guard_bypass
@pytest.mark.skipif(not HAS_FLOCK, reason="race serialization requires flock")
def test_install_sh_dead_owner_reclamation_serializes_stale_race(
    tmp_path: Path,
) -> None:
    real_ln = shutil.which("ln")
    real_rm = shutil.which("rm")
    assert real_ln is not None
    assert real_rm is not None

    home = tmp_path / "home"
    home.mkdir()
    former_owner = subprocess.Popen(["sleep", "30"])
    dead_pid = former_owner.pid
    former_owner.terminate()
    former_owner.wait(timeout=5)
    marker = home / ".hermes-update-in-progress"
    marker.write_text(f"{dead_pid}\n1\n", encoding="utf-8")

    # Force both contenders past their initial no-clobber publication while
    # the dead generation still exists. The rm wrapper then deterministically
    # recreates the old check/unlink race: without the shared mutex, contender
    # B waits until A has published before unlinking A's live generation. With
    # the mutex, only one contender can reach marker removal and the wait
    # simply times out before that sole owner publishes.
    barrier = tmp_path / "barrier"
    barrier.mkdir()
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    ln_wrapper = wrapper_dir / "ln"
    ln_wrapper.write_text(
        "#!/bin/bash\n"
        "last=''\n"
        'for arg in "$@"; do last="$arg"; done\n'
        'if [ "$last" = "$HERMES_RACE_MARKER" ]; then\n'
        '  first="$HERMES_RACE_BARRIER/$HERMES_RACE_WORKER.first-ln"\n'
        '  if [ ! -e "$first" ]; then\n'
        '    : > "$first"\n'
        '    : > "$HERMES_RACE_BARRIER/$HERMES_RACE_WORKER.ready"\n'
        "    i=0\n"
        '    while [ ! -e "$HERMES_RACE_BARRIER/a.ready" ] || '
        '[ ! -e "$HERMES_RACE_BARRIER/b.ready" ]; do\n'
        "      i=$((i + 1))\n"
        "      [ \"$i\" -lt 40 ] || exit 70\n"
        "      sleep 0.05\n"
        "    done\n"
        f'    exec "{real_ln}" "$@"\n'
        "  fi\n"
        f'  "{real_ln}" "$@"\n'
        "  rc=$?\n"
        '  if [ "$rc" -eq 0 ] && [ "$HERMES_RACE_WORKER" = a ]; then\n'
        '    : > "$HERMES_RACE_BARRIER/a.published"\n'
        "  fi\n"
        '  exit "$rc"\n'
        "fi\n"
        'case "$last" in "$HERMES_RACE_MARKER".stale.snapshot.*)\n'
        '  : > "$HERMES_RACE_BARRIER/$HERMES_RACE_WORKER.snapshot"\n'
        "  i=0\n"
        '  while [ ! -e "$HERMES_RACE_BARRIER/a.snapshot" ] || '
        '[ ! -e "$HERMES_RACE_BARRIER/b.snapshot" ]; do\n'
        "    i=$((i + 1))\n"
        "    [ \"$i\" -lt 20 ] || break\n"
        "    sleep 0.05\n"
        "  done\n"
        ";; esac\n"
        f'exec "{real_ln}" "$@"\n',
        encoding="utf-8",
    )
    ln_wrapper.chmod(0o755)

    rm_wrapper = wrapper_dir / "rm"
    rm_wrapper.write_text(
        "#!/bin/bash\n"
        "last=''\n"
        'for arg in "$@"; do last="$arg"; done\n'
        'if [ "$last" = "$HERMES_RACE_MARKER" ]; then\n'
        '  : > "$HERMES_RACE_BARRIER/$HERMES_RACE_WORKER.at-rm"\n'
        "  sleep 0.25\n"
        '  if [ -e "$HERMES_RACE_BARRIER/a.at-rm" ] && '
        '[ -e "$HERMES_RACE_BARRIER/b.at-rm" ] && '
        '[ "$HERMES_RACE_WORKER" = b ]; then\n'
        "    i=0\n"
        '    while [ ! -e "$HERMES_RACE_BARRIER/a.published" ]; do\n'
        "      i=$((i + 1))\n"
        "      [ \"$i\" -lt 50 ] || exit 71\n"
        "      sleep 0.05\n"
        "    done\n"
        "  fi\n"
        "fi\n"
        f'exec "{real_rm}" "$@"\n',
        encoding="utf-8",
    )
    rm_wrapper.chmod(0o755)

    winners = tmp_path / "winners"
    command = (
        'installer=$1; home=$2; winners=$3; worker=$4; set --; HERMES_HOME="$home"; '
        'source "$installer"; '
        'mutate() { printf "%s\\n" "$worker" >> "$winners"; sleep 1; }; '
        "run_installer_with_update_lock mutate"
    )
    processes: list[subprocess.Popen[str]] = []
    for worker in ("a", "b"):
        env = os.environ | {
            "HERMES_RACE_BARRIER": str(barrier),
            "HERMES_RACE_MARKER": str(marker),
            "HERMES_RACE_WORKER": worker,
            "PATH": f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}",
        }
        processes.append(
            subprocess.Popen(
                [
                    "bash",
                    "-c",
                    command,
                    "stale-race-test",
                    str(INSTALL_SH),
                    str(home),
                    str(winners),
                    worker,
                ],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        )

    try:
        outputs = [process.communicate(timeout=30) for process in processes]
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)

    assert sorted(process.returncode for process in processes) == [0, 2], outputs
    assert winners.read_text(encoding="utf-8").splitlines() in (["a"], ["b"])
    assert not marker.exists()
    assert not list(home.glob(".hermes-update-in-progress.*.claim.*"))
    assert not list(home.glob(".hermes-update-in-progress.stale.snapshot.*"))


@pytest.mark.live_system_guard_bypass
def test_install_sh_lock_does_not_change_caller_umask(tmp_path: Path) -> None:
    home = tmp_path / "home"
    observed = tmp_path / "umask"
    command = (
        'installer=$1; home=$2; observed=$3; set --; HERMES_HOME="$home"; '
        'source "$installer"; umask 0027; '
        'record_umask() { umask > "$observed"; }; '
        'run_installer_with_update_lock record_umask'
    )

    result = subprocess.run(
        ["bash", "-c", command, "umask-test", str(INSTALL_SH), str(home), str(observed)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout
    assert observed.read_text(encoding="utf-8").strip() == "0027"


@pytest.mark.live_system_guard_bypass
def test_install_sh_pid_probe_preserves_disabled_errexit(tmp_path: Path) -> None:
    command = (
        'installer=$1; set --; source "$installer"; set +e; '
        'install_pid_state "$$" >/dev/null; case "$-" in *e*) exit 99;; esac'
    )

    result = subprocess.run(
        ["bash", "-c", command, "errexit-test", str(INSTALL_SH)],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr + result.stdout


@pytest.mark.live_system_guard_bypass
def test_install_sh_lock_uses_bsd_portable_mktemp_template(tmp_path: Path) -> None:
    real_mktemp = shutil.which("mktemp")
    assert real_mktemp is not None
    home = tmp_path / "home"
    wrapper_dir = tmp_path / "bin"
    wrapper_dir.mkdir()
    wrapper = wrapper_dir / "mktemp"
    wrapper.write_text(
        "#!/bin/bash\n"
        "last=''\n"
        'for arg in "$@"; do last="$arg"; done\n'
        'case "$last" in *XXXXXX) ;; *) exit 64 ;; esac\n'
        f'exec "{real_mktemp}" "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)

    result = subprocess.run(
        ["bash", str(INSTALL_SH), "--stage", "not-a-stage", "--non-interactive"],
        env=os.environ
        | {"HERMES_HOME": str(home), "PATH": f"{wrapper_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "Unknown stage" in result.stdout
    assert "Could not stage" not in result.stdout
