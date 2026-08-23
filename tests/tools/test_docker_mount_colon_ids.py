"""Regression tests for #92414/#93044 — colon-bearing session ids must not
reach Docker `-v` mount specs.

Docker's short `-v host:container[:mode]` syntax splits on ':'. Session ids
like `session:agent:main:telegram:dm:<id>` therefore made every sandboxed
tool call fail with exit 125 ("invalid mode: /root") when
``terminal.container_persistent`` is enabled, because the raw id was used as
a bind-mount host-path component. Container labels were already sanitized;
the mount path was not.

The fix routes the persistent-sandbox path component through the same
sanitizer used for labels, so one safe form derives both.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def docker_env_cls(monkeypatch, tmp_path):
    """Import tools.environments.docker with HERMES_HOME pointed at tmp."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(sys, "path", [str(REPO_ROOT)] + sys.path)
    import importlib

    if "hermes_constants" in sys.modules:
        importlib.reload(sys.modules["hermes_constants"])
    else:
        __import__("hermes_constants")
    mod = importlib.import_module("tools.environments.docker")
    return mod.DockerEnvironment, mod._sanitize_label_value


COLON_ID = "session:agent:main:telegram:dm:12345"


def _capture_mounts(docker_env_cls, task_id):
    """Build a DockerEnvironment with subprocess.run intercepted; return
    (env, list of -v spec strings)."""
    mod = sys.modules["tools.environments.docker"]
    captured: list[list] = []

    class FakeCompleted:
        def __init__(self, out: bytes = b""):
            self.stdout = out
            self.returncode = 0

    def fake_run(*args, **kwargs):
        cmd = args[0] if args else kwargs.get("args")
        captured.append(list(cmd or []))
        if any(x == "ps" for x in (cmd or [])):
            return FakeCompleted(b"")
        return FakeCompleted(b"fake-container-id\n")

    orig_run = mod.subprocess.run
    mod.subprocess.run = fake_run
    try:
        env = docker_env_cls(
            image="ubuntu:24.04", task_id=task_id, persistent_filesystem=True,
        )
    finally:
        mod.subprocess.run = orig_run

    mounts = []
    for cmd in captured:
        for i, c in enumerate(cmd):
            if c == "-v":
                mounts.append(cmd[i + 1])
    return env, mounts


def test_colon_session_id_produces_parsable_mounts(docker_env_cls):
    """THE bug: a colon-bearing id must never leak into a -v host path."""
    DockerEnvironment, _ = docker_env_cls
    env, mounts = _capture_mounts(DockerEnvironment, COLON_ID)
    assert mounts, "expected at least one -v mount"
    colon_hosts = [m for m in mounts if ":" in m.split(":")[0]]
    assert colon_hosts == [], (
        f"-v host paths contain colons — unparseable by docker: {colon_hosts}"
    )


def test_sanitized_id_matches_label_form(docker_env_cls):
    """Path component and label should derive from the same sanitized form."""
    DockerEnvironment, sanitize = docker_env_cls
    env, _mounts = _capture_mounts(DockerEnvironment, COLON_ID)
    assert ":" not in env._home_dir
    expected_fragment = sanitize(COLON_ID)
    assert expected_fragment in env._home_dir, (
        f"sandbox path {env._home_dir!r} does not contain the sanitized "
        f"id fragment {expected_fragment!r}"
    )


def test_plain_task_id_path_unchanged(docker_env_cls, tmp_path):
    """Ids without colons keep working; no behavior change for them."""
    DockerEnvironment, _ = docker_env_cls
    env, mounts = _capture_mounts(DockerEnvironment, "default")
    assert ":" not in Path(env._home_dir).name
    assert any(m.endswith(":/root") for m in mounts), (
        "persistent home mount missing"
    )
