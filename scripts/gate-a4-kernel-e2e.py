from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from hermes_cli import kanban_containment as containment


def wait_until(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.05)
    return bool(predicate())


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: gate-a4-kernel-e2e.py ROOT")
    root = Path(sys.argv[1])
    inode = root.stat(follow_symlinks=False).st_ino
    os.environ["HERMES_KANBAN_CGROUP_ROOT"] = str(root)
    os.environ["HERMES_KANBAN_CGROUP_ROOT_INODE"] = str(inode)
    marker = Path(f"/tmp/hermes-a4-gate-{os.getpid()}.json")
    worker_log = marker.with_suffix(".log")
    target = (
        "import json,os,subprocess,sys,time;"
        "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(300)']);"
        f"open({str(marker)!r},'w').write(json.dumps([os.getpid(),p.pid]));"
        "p.wait()"
    )
    handle = None
    log_handle = open(worker_log, "wb")
    try:
        handle = containment.spawn_gated(
            [sys.executable, "-c", target],
            task_id="t_a4_kernel_e2e",
            run_id=424242,
            claim_lock="host:a4-kernel-e2e",
            popen_kwargs={
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "env": dict(os.environ),
                "cwd": "/src" if Path("/src/hermes_cli").is_dir() else None,
                "start_new_session": True,
            },
        )
        log_handle.close()
        if marker.exists():
            raise RuntimeError("worker executed before durable gate release")
        handle.release()
        if not wait_until(marker.exists):
            process_rc = handle._process.poll()
            diagnostic = worker_log.read_text(encoding="utf-8", errors="replace")
            raise RuntimeError(
                f"released worker did not execute rc={process_rc}: {diagnostic}"
            )
        leader, descendant = json.loads(marker.read_text(encoding="utf-8"))
        members = {
            int(value)
            for value in Path(handle.cgroup_path)
            .joinpath("cgroup.procs")
            .read_text(encoding="ascii")
            .splitlines()
        }
        if not {leader, descendant}.issubset(members):
            raise RuntimeError(f"process tree escaped worker cgroup: {members}")
        termination = containment.kill_cgroup(
            handle.cgroup_path, handle.cgroup_inode, wait_seconds=5.0
        )
        if not termination.get("containment_certified"):
            raise RuntimeError(f"termination uncertified: {termination}")
        if containment.cgroup_populated(handle.cgroup_path, handle.cgroup_inode):
            raise RuntimeError("worker cgroup remains populated")
        if not containment.cleanup_cgroup(handle.cgroup_path, handle.cgroup_inode):
            raise RuntimeError("exact empty cgroup cleanup failed")
        print(
            json.dumps(
                {
                    "passed": True,
                    "gate_blocked_before_release": True,
                    "leader": leader,
                    "descendant": descendant,
                    "termination": termination,
                    "cleanup": True,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        if not log_handle.closed:
            log_handle.close()
        marker.unlink(missing_ok=True)
        worker_log.unlink(missing_ok=True)
        if handle is not None and Path(handle.cgroup_path).exists():
            containment.kill_cgroup(
                handle.cgroup_path, handle.cgroup_inode, wait_seconds=2.0
            )
            containment.cleanup_cgroup(handle.cgroup_path, handle.cgroup_inode)


if __name__ == "__main__":
    raise SystemExit(main())
