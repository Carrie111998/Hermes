"""Tests for the skill-sync optional skill.

Validates:
  - SKILL.md frontmatter conforms to the authoring standards
  - No personal machine names / logins leak into the shipped skill
  - sync.sh behaves correctly end-to-end against two local "machines"
    (HERMES_HOME-style skill trees synced over a loopback SSH stub)
  - doctor.sh diagnoses missing prerequisites with actionable fixes
  - p2p_sync.py classifies divergent copies without transferring anything
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path

import pytest
import yaml

SKILL_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "devops"
    / "skill-sync"
)
SCRIPTS = SKILL_DIR / "scripts"


@pytest.fixture(scope="module")
def skill_source() -> str:
    return (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def frontmatter(skill_source) -> dict:
    m = re.search(r"^---\n(.*?)\n---", skill_source, re.DOTALL)
    assert m, "SKILL.md missing YAML frontmatter"
    return yaml.safe_load(m.group(1))


# ---------------------------------------------------------------------------
# Frontmatter / authoring standards
# ---------------------------------------------------------------------------

def test_skill_dir_exists() -> None:
    assert SKILL_DIR.is_dir()


def test_description_under_60_chars(frontmatter) -> None:
    desc = frontmatter["description"]
    assert len(desc) <= 60, f"description is {len(desc)} chars: {desc!r}"


def test_required_frontmatter_fields(frontmatter) -> None:
    for field in ("name", "description", "version", "author", "license", "platforms"):
        assert field in frontmatter, f"missing required field: {field}"
    assert frontmatter["name"] == "skill-sync"
    assert set(frontmatter["platforms"]) == {"linux", "macos"}


def test_no_hardcoded_remotes() -> None:
    """Shipped scripts must not bake in anyone's machines: every user@host
    occurrence must be a generic placeholder."""
    allowed = re.compile(
        r"(user@(host2?|newbox|workstation|laptop|testbox)"
        r"|<user>@|<user@host>|<candidate>@|user@box\d)"
    )
    remote_like = re.compile(r"[A-Za-z0-9_.<>-]+@[A-Za-z0-9_.<>-]+")
    for path in sorted(SKILL_DIR.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in remote_like.finditer(text):
            token = m.group(0)
            # emails and generic placeholders are fine
            if "." in token.split("@")[1] or allowed.search(token):
                continue
            assert allowed.search(token), (
                f"possible hardcoded remote {token!r} in {path.name}"
            )


def test_scripts_shipped_and_referenced(skill_source) -> None:
    for script in ("sync.sh", "doctor.sh", "p2p_sync.py"):
        assert (SCRIPTS / script).is_file(), f"missing scripts/{script}"
        assert script in skill_source, f"SKILL.md never mentions {script}"
    assert (SKILL_DIR / "references" / "merging-forked-skill-copies.md").is_file()


def test_sync_sh_requires_explicit_remotes() -> None:
    """No baked-in default remotes: bare invocation must fail with usage."""
    env = {k: v for k, v in os.environ.items() if k != "SKILL_SYNC_REMOTES"}
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "sync.sh")],
        capture_output=True, text=True, timeout=30, env=env,
    )
    assert proc.returncode != 0
    assert "No remotes configured" in proc.stdout + proc.stderr


# ---------------------------------------------------------------------------
# E2E harness: fake "remote" via an ssh/rsync-intercepting PATH shim
# ---------------------------------------------------------------------------
#
# sync.sh shells out to `ssh <opts> <remote> <cmd>` and
# `rsync -azL [-e ssh...] src dst`. We put stub ssh/rsync executables first on
# PATH: ssh runs the command locally against a fake remote HERMES_HOME; rsync
# strips `remote:` prefixes and falls through to a local copy. This exercises
# the real control flow (listing, mtime compare, counters, verification)
# without a network.


def _make_skill(root: Path, cat: str, name: str, body: str, mtime: int) -> None:
    d = root / "skills" / cat / name
    d.mkdir(parents=True, exist_ok=True)
    f = d / "SKILL.md"
    f.write_text(body, encoding="utf-8")
    os.utime(f, (mtime, mtime))
    os.utime(d, (mtime, mtime))


@pytest.fixture()
def fake_fleet(tmp_path):
    """Two HERMES_HOMEs (local + 'remote') plus PATH shims for ssh/rsync."""
    local_home = tmp_path / "local-hermes"
    remote_home = tmp_path / "remote-hermes"
    (local_home / "skills").mkdir(parents=True)
    (remote_home / "skills").mkdir(parents=True)

    now = int(time.time())
    # remote-newer skill (should be pulled)
    _make_skill(local_home, "devops", "alpha", "old alpha\n", now - 7200)
    _make_skill(remote_home, "devops", "alpha", "new alpha\n", now - 60)
    # remote-only skill (should be pulled as new)
    _make_skill(remote_home, "research", "beta", "beta content\n", now - 60)
    # local-newer skill (pull must NOT touch it; push should send it)
    _make_skill(local_home, "devops", "gamma", "local gamma newer\n", now - 60)
    _make_skill(remote_home, "devops", "gamma", "remote gamma old\n", now - 7200)
    # remote archived skill (must be excluded from pull)
    _make_skill(remote_home, ".archive", "old-junk", "archived\n", now - 60)
    # local backup copy (must be excluded from push)
    _make_skill(local_home, "devops", "gamma.bak", "backup copy\n", now - 30)

    bindir = tmp_path / "bin"
    bindir.mkdir()

    import shutil

    real_rsync = shutil.which("rsync")
    assert real_rsync, "rsync required for this test"

    ssh_stub = bindir / "ssh"
    ssh_stub.write_text(
        "#!/usr/bin/env bash\n"
        "# consume ssh options; the last two args are <remote> <command...>\n"
        "args=()\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -o) shift 2 ;;\n"
        "    *) args+=(\"$1\"); shift ;;\n"
        "  esac\n"
        "done\n"
        "# args[0] = remote, rest = command\n"
        f"export HERMES_HOME=\"{remote_home}\"\n"
        "export HOME=\"$HOME\"\n"
        "cmd=\"${args[*]:1}\"\n"
        "exec bash -c \"$cmd\"\n",
        encoding="utf-8",
    )
    ssh_stub.chmod(ssh_stub.stat().st_mode | stat.S_IEXEC)

    rsync_stub = bindir / "rsync"
    rsync_stub.write_text(
        "#!/usr/bin/env bash\n"
        "# strip -e '<ssh cmd>' and remote: prefixes, then local rsync\n"
        f"real=\"{real_rsync}\"\n"
        "args=()\n"
        "while [ $# -gt 0 ]; do\n"
        "  case \"$1\" in\n"
        "    -e) shift 2 ;;\n"
        "    *) args+=(\"${1#*:}\"); shift ;;\n"
        "  esac\n"
        "done\n"
        "exec \"$real\" \"${args[@]}\"\n",
        encoding="utf-8",
    )
    rsync_stub.chmod(rsync_stub.stat().st_mode | stat.S_IEXEC)

    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["HERMES_HOME"] = str(local_home)
    # The ssh stub sets HERMES_HOME for "remote" commands, but rsync paths
    # produced by sync.sh embed the remote skills dir absolutely, so the
    # stubbed transfers land in the right tree.
    return {"local": local_home, "remote": remote_home, "env": env}


def _run_sync(fleet, *args):
    return subprocess.run(
        ["bash", str(SCRIPTS / "sync.sh"), *args],
        capture_output=True, text=True, timeout=60, env=fleet["env"],
    )


def test_e2e_pull_updates_and_news(fake_fleet) -> None:
    proc = _run_sync(fake_fleet, "user@testbox")
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "[^] alpha" in out
    assert "[+] beta" in out
    # archived remote skills are never pulled
    assert "old-junk" not in out
    local = fake_fleet["local"]
    assert not (local / "skills/.archive").exists()
    # local-newer gamma must not be pulled over
    assert (local / "skills/devops/alpha/SKILL.md").read_text() == "new alpha\n"
    assert (local / "skills/research/beta/SKILL.md").read_text() == "beta content\n"
    assert (local / "skills/devops/gamma/SKILL.md").read_text() == "local gamma newer\n"


def test_e2e_pull_dry_run_transfers_nothing(fake_fleet) -> None:
    proc = _run_sync(fake_fleet, "user@testbox")
    # first run synced; touch remote alpha newer again, then dry-run
    remote_alpha = fake_fleet["remote"] / "skills/devops/alpha/SKILL.md"
    remote_alpha.write_text("even newer alpha\n", encoding="utf-8")
    future = int(time.time()) + 10
    os.utime(remote_alpha, (future, future))

    proc = _run_sync(fake_fleet, "user@testbox")
    assert "[^] alpha" in proc.stdout

    # DRY_RUN must report but not transfer
    remote_alpha.write_text("dry run bait\n", encoding="utf-8")
    os.utime(remote_alpha, (future + 100, future + 100))
    env = dict(fake_fleet["env"])
    env["DRY_RUN"] = "1"
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "sync.sh"), "user@testbox"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert "[^] alpha" in proc.stdout
    assert "Dry run" in proc.stdout
    local_alpha = fake_fleet["local"] / "skills/devops/alpha/SKILL.md"
    assert local_alpha.read_text() != "dry run bait\n"


def test_e2e_push_sends_local_newer_and_verifies(fake_fleet) -> None:
    proc = _run_sync(fake_fleet, "--push", "user@testbox")
    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "[^] gamma" in out
    # backup copies are never pushed
    assert "gamma.bak" not in out
    remote = fake_fleet["remote"]
    assert not (remote / "skills/devops/gamma.bak").exists()
    assert (remote / "skills/devops/gamma/SKILL.md").read_text() == "local gamma newer\n"
    # push must print a remote-count verification line
    assert "Verify: remote now has" in out


def test_doctor_reports_connection_refused_fix(tmp_path) -> None:
    """doctor.sh must translate 'Connection refused' into the Remote Login fix."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh_stub = bindir / "ssh"
    ssh_stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'ssh: connect to host testbox port 22: Connection refused' >&2\n"
        "exit 255\n",
        encoding="utf-8",
    )
    ssh_stub.chmod(ssh_stub.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "doctor.sh"), "user@testbox"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert proc.returncode == 1
    assert "sshd not listening" in proc.stdout
    assert "Remote Login" in proc.stdout


def test_doctor_reports_permission_denied_fix(tmp_path) -> None:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh_stub = bindir / "ssh"
    ssh_stub.write_text(
        "#!/usr/bin/env bash\n"
        "echo 'user@testbox: Permission denied (publickey).' >&2\n"
        "exit 255\n",
        encoding="utf-8",
    )
    ssh_stub.chmod(ssh_stub.stat().st_mode | stat.S_IEXEC)
    env = dict(os.environ)
    env["PATH"] = f"{bindir}:{env['PATH']}"
    proc = subprocess.run(
        ["bash", str(SCRIPTS / "doctor.sh"), "user@testbox"],
        capture_output=True, text=True, timeout=60, env=env,
    )
    assert proc.returncode == 1
    assert "key NOT authorized" in proc.stdout
    assert "ssh-copy-id" in proc.stdout


def test_p2p_sync_classify_divergent(tmp_path) -> None:
    """classify() must call a genuine fork 'divergent', not 'superset'."""
    import importlib.util
    import sys

    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location("p2p_sync", SCRIPTS / "p2p_sync.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = False

    a = tmp_path / "a.md"
    b = tmp_path / "b.md"
    a.write_text("line1\nline2\nlocal-only\n")
    b.write_text("line1\nline2\nremote-only\n")
    assert mod.classify(str(a), str(b)) == "divergent"

    b.write_text("line1\nline2\nlocal-only\nplus-more\n")
    assert mod.classify(str(a), str(b)) == "superset"

    b.write_text("line1\nline2\nlocal-only\n")
    assert mod.classify(str(a), str(b)) == "identical"
