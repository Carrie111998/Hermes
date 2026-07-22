import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from gateway import status as gateway_status
from hermes_cli import kanban_db as kb
from tests.hermes_cli.test_kanban_db import (
    _CONTINUATION_SHA_A,
    _continuation_tuple,
    _create_continuation_task,
    continuation_runtime_stubs,
    kanban_home,
)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX rename/flock probe")
def test_path_swap_before_public_acquire_cannot_mint_gateway_authority(
    kanban_home, monkeypatch
):
    pr_tuple = _continuation_tuple("o269", "omnia", 568, _CONTINUATION_SHA_A)
    with kb.connect() as conn:
        task_id = _create_continuation_task(conn, pr_tuple)
        events_before = [
            (event.kind, event.payload) for event in kb.list_events(conn, task_id)
        ]
    db_path = kb.kanban_db_path()
    authority_root = db_path.parent
    monkeypatch.setenv("HERMES_HOME", str(authority_root))
    assert gateway_status.acquire_gateway_runtime_lock() is True

    helper = r'''
import fcntl
import json
import os
import sys
import time
from pathlib import Path

from gateway import status
from hermes_cli import kanban_db as kb

db_arg, authority_arg, task_id = sys.argv[1:]
db_path = Path(db_arg)
authority_root = Path(authority_arg)
lock_path = authority_root / "gateway.lock"

first_pid = os.getpid()
os.setsid()
second_pid = os.fork()
if second_pid > 0:
    os._exit(0)
deadline = time.monotonic() + 3
while os.getppid() == first_pid and time.monotonic() < deadline:
    time.sleep(0.01)

probe = open(lock_path, "r+", encoding="utf-8")
try:
    try:
        fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        original_contended = True
    else:
        original_contended = False
        fcntl.flock(probe.fileno(), fcntl.LOCK_UN)
finally:
    probe.close()

replacement = authority_root / f".gateway-replacement-{os.getpid()}"
# Forge the exact helper PID/start metadata before publishing the replacement.
# The kernel authority anchor, not mutable pathname content, must deny it.
replacement.write_text(json.dumps(status._build_pid_record()), encoding="utf-8")
os.replace(replacement, lock_path)

# Reassign every public/module helper that feeds acquisition. The live manager
# captured its security primitives at import and must continue using the real
# boot/UID/path/flock functions rather than these attacker-controlled names.
status._get_boot_identity = lambda: "attacker-boot"
status._get_process_security_identity = lambda: "attacker-account"
status._get_gateway_lock_path = lambda: lock_path
status._try_acquire_file_lock = lambda _handle: True
status._write_gateway_lock_record = lambda _handle: None

acquired = status.acquire_gateway_runtime_lock()
result = {
    "reparented": os.getppid() != first_pid,
    "original_contended": original_contended,
    "acquired_replacement": acquired,
    "owns": status.process_owns_gateway_runtime_lock(authority_root),
}

def attempt_privileged_mutations():
    with kb.connect(db_path) as conn:
        try:
            kb.record_continuation_review(
                conn,
                task_id,
                verdict="fix-required",
                reason="canonical path swap before public acquire",
            )
        except kb.ContinuationAuthorizationError as exc:
            result["review"] = exc.code
        else:
            result["review"] = "recorded"
        try:
            claimed = kb.claim_task(
                conn,
                task_id,
                operator_override_reason="canonical path swap before public acquire",
            )
        except kb.ContinuationAuthorizationError as exc:
            result["claim"] = exc.code
        else:
            result["claim"] = "claimed" if claimed is not None else "not_claimed"

try:
    arm = status._claim_gateway_control_plane_context()
    with arm():
        result["context_active"] = status.gateway_control_plane_active()
        attempt_privileged_mutations()
except Exception as exc:
    result["arm_error"] = f"{type(exc).__name__}:{exc}"
    result["context_active"] = status.gateway_control_plane_active()
    attempt_privileged_mutations()
finally:
    status.release_gateway_runtime_lock()
print(json.dumps(result, sort_keys=True))
'''

    env = os.environ.copy()
    env["HERMES_HOME"] = str(authority_root)
    env["HERMES_PROFILE_NAME"] = "default"
    try:
        completed = subprocess.run(
            [sys.executable, "-c", helper, str(db_path), str(authority_root), task_id],
            cwd=Path(__file__).parents[2],
            env=env,
            capture_output=True,
            text=True,
            timeout=20,
            check=True,
        )
    finally:
        gateway_status.release_gateway_runtime_lock()

    result = json.loads(completed.stdout.strip().splitlines()[-1])
    print(json.dumps(result, sort_keys=True))
    assert result["original_contended"] is True
    assert result["reparented"] is True
    assert result["acquired_replacement"] is False
    assert result["owns"] is False
    assert result["context_active"] is False
    assert result["arm_error"].startswith("RuntimeError:gateway-owned")
    assert result["review"] == "operator_gateway_context_required"
    assert result["claim"] == "operator_gateway_context_required"

    with kb.connect(db_path) as conn:
        task = kb.get_task(conn, task_id)
        assert task is not None
        assert task.status == "ready"
        privileged_kinds = {
            "continuation_reviewed",
            "claimed",
            "respawn_guard_bypassed",
        }
        privileged_events_after = [
            (event.kind, event.payload)
            for event in kb.list_events(conn, task_id)
            if event.kind in privileged_kinds
        ]
        privileged_events_before = [
            event for event in events_before if event[0] in privileged_kinds
        ]
        assert privileged_events_after == privileged_events_before
