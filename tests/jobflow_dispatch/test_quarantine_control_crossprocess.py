"""Real subprocess proof for the dispatch byte-lock boundary."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time

import pytest


def _env(repo: Path) -> dict[str, str]:
    env = os.environ.copy()
    inherited = [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(repo), *(p for p in inherited if p != str(repo))]
    )
    return env


@pytest.mark.timeout(60)
def test_barrier_waits_for_actor_in_another_process(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    db = tmp_path / "dispatch.db"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys,time; "
            "from jobflow_dispatch.quarantine_control import QuarantineControlStore; "
            "s=QuarantineControlStore(Path(sys.argv[1]), poll_interval=.005); "
            "cm=s.dispatch_section(boundary='child'); cm.__enter__(); "
            "Path(sys.argv[2]).write_text('ready'); "
            "\nwhile not Path(sys.argv[3]).exists(): time.sleep(.005)\n"
            "cm.__exit__(None,None,None)",
            str(db),
            str(ready),
            str(release),
        ],
        cwd=repo,
        env=_env(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), child.communicate(timeout=2)

    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    store = QuarantineControlStore(db, timeout=0.05, poll_interval=0.005)
    with pytest.raises(TimeoutError, match="dispatch sections"):
        with store.acquire_dispatch_barrier(reason="incident"):
            pass

    release.write_text("release", encoding="utf-8")
    stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 0, f"stdout={stdout}\nstderr={stderr}"
    with store.acquire_dispatch_barrier(reason="retry", timeout=1) as barrier:
        assert barrier.assert_held()["complete"] is True


@pytest.mark.timeout(60)
def test_process_death_releases_actor_admission(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    db = tmp_path / "dispatch.db"
    ready = tmp_path / "ready"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys,time; "
            "from jobflow_dispatch.quarantine_control import QuarantineControlStore; "
            "s=QuarantineControlStore(Path(sys.argv[1]), poll_interval=.005); "
            "cm=s.dispatch_section(boundary='child'); cm.__enter__(); "
            "Path(sys.argv[2]).write_text('ready'); time.sleep(60)",
            str(db),
            str(ready),
        ],
        cwd=repo,
        env=_env(repo),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    deadline = time.monotonic() + 20
    while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists(), child.communicate(timeout=2)

    child.kill()
    child.communicate(timeout=20)

    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    store = QuarantineControlStore(db, timeout=1, poll_interval=0.005)
    with store.acquire_dispatch_barrier(reason="after-owner-death") as barrier:
        assert barrier.assert_held()["complete"] is True


@pytest.mark.timeout(60)
def test_active_fence_refuses_actor_in_another_process(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    db = tmp_path / "dispatch.db"
    from jobflow_dispatch.quarantine_control import QuarantineControlStore

    store = QuarantineControlStore(db, poll_interval=0.005)
    with store.acquire_dispatch_barrier(reason="incident") as barrier:
        token = store.activate_fence(
            barrier_token=barrier.token,
            authorization_request_id="auth-1",
            required=True,
        )["post"]["fence_token"]

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from jobflow_dispatch.quarantine_control import QuarantineControlStore; "
            "s=QuarantineControlStore(Path(sys.argv[1])); "
            "\ntry:\n with s.dispatch_section(boundary='child'): pass\n"
            "except RuntimeError as e:\n print(str(e)); raise SystemExit(0)\n"
            "raise SystemExit(2)",
            str(db),
        ],
        cwd=repo,
        env=_env(repo),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    assert "dispatch fenced" in child.stdout
    assert store.verify_fence(token)["fence_token"] == token


@pytest.mark.timeout(60)
def test_simultaneous_first_open_converges_on_one_durable_identity(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    db = tmp_path / "dispatch.db"
    start = tmp_path / "start"
    children = []
    for index in range(8):
        children.append(
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; import json,sys,time; "
                    "from jobflow_dispatch.quarantine_control import QuarantineControlStore; "
                    "\nwhile not Path(sys.argv[2]).exists(): time.sleep(.002)\n"
                    "s=QuarantineControlStore(Path(sys.argv[1]), timeout=10, poll_interval=.002); "
                    "print(json.dumps({'database': s._database_identity, "
                    "'lock': s._lock_identity, 'fence': s.fence_state()}))",
                    str(db),
                    str(start),
                ],
                cwd=repo,
                env=_env(repo),
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        )
    start.write_text("start", encoding="utf-8")

    results = []
    for child in children:
        stdout, stderr = child.communicate(timeout=30)
        assert child.returncode == 0, f"stdout={stdout}\nstderr={stderr}"
        results.append(json.loads(stdout))

    assert len({tuple(result["database"]) for result in results}) == 1
    assert len({tuple(result["lock"]) for result in results}) == 1
    assert all(result["fence"]["fenced"] is False for result in results)
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM quarantine_control_identity"
        ).fetchone()[0] == 1


@pytest.mark.timeout(60)
def test_fresh_process_refuses_replaced_database(tmp_path):
    import shutil

    repo = Path(__file__).resolve().parents[2]
    db = tmp_path / "dispatch.db"
    creator = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from jobflow_dispatch.quarantine_control import QuarantineControlStore; "
            "QuarantineControlStore(Path(sys.argv[1]))",
            str(db),
        ],
        cwd=repo,
        env=_env(repo),
        text=True,
        capture_output=True,
        timeout=30,
    )
    assert creator.returncode == 0, creator.stderr

    original_identity = (db.stat().st_dev, db.stat().st_ino)
    replacement = tmp_path / "replacement.db"
    shutil.copyfile(db, replacement)
    os.replace(replacement, db)

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from jobflow_dispatch.quarantine_control import QuarantineControlStore; "
            "\ntry: QuarantineControlStore(Path(sys.argv[1]))\n"
            "except Exception as e: print(type(e).__name__ + ': ' + str(e)); raise SystemExit(0)\n"
            "raise SystemExit(2)",
            str(db),
        ],
        cwd=repo,
        env=_env(repo),
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert child.returncode == 0, child.stderr
    assert "canonical dispatch control database identity changed" in child.stdout
    assert original_identity != (db.stat().st_dev, db.stat().st_ino)


@pytest.mark.timeout(60)
def test_fresh_process_refuses_replaced_lock_file(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    db = tmp_path / "dispatch.db"
    from jobflow_dispatch import quarantine_control as control

    store = control.QuarantineControlStore(db)
    replacement = tmp_path / "replacement.lock"
    replacement.write_bytes(b"0" * control._CONTROL_LOCK_FILE_SIZE)
    os.replace(replacement, store.lock_path)

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import sys; "
            "from jobflow_dispatch.quarantine_control import QuarantineControlStore; "
            "\ntry: QuarantineControlStore(Path(sys.argv[1]))\n"
            "except Exception as e: print(type(e).__name__ + ': ' + str(e)); raise SystemExit(0)\n"
            "raise SystemExit(2)",
            str(db),
        ],
        cwd=repo,
        env=_env(repo),
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert child.returncode == 0, child.stderr
    assert "canonical dispatch control identity changed" in child.stdout


@pytest.mark.timeout(60)
def test_pre_identity_control_database_is_migrated_once_under_exclusion(tmp_path):
    repo = Path(__file__).resolve().parents[2]
    db = tmp_path / "dispatch.db"
    lock = db.with_suffix(".dispatch.lock")
    lock.write_bytes(b"0" * 129)
    with sqlite3.connect(db) as conn:
        conn.executescript(
            """
            CREATE TABLE quarantine_dispatch_fence (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                fenced INTEGER NOT NULL CHECK(fenced IN (0, 1)),
                generation INTEGER NOT NULL,
                fence_token TEXT,
                authorization_request_id TEXT,
                changed_at TEXT NOT NULL
            );
            INSERT INTO quarantine_dispatch_fence VALUES
                (1, 0, 7, NULL, NULL, '2026-08-21T00:00:00Z');
            CREATE TABLE quarantine_wakes (
                job_id TEXT PRIMARY KEY,
                wake_token TEXT NOT NULL UNIQUE,
                caller TEXT NOT NULL,
                reason TEXT,
                requested_at TEXT NOT NULL
            );
            """
        )

    child = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; import json,sys; "
            "from jobflow_dispatch.quarantine_control import QuarantineControlStore; "
            "s=QuarantineControlStore(Path(sys.argv[1])); print(json.dumps(s.fence_state()))",
            str(db),
        ],
        cwd=repo,
        env=_env(repo),
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert child.returncode == 0, child.stderr
    assert json.loads(child.stdout)["generation"] == 7
    with sqlite3.connect(db) as conn:
        identity = conn.execute(
            "SELECT database_device, database_inode, lock_device, lock_inode "
            "FROM quarantine_control_identity WHERE singleton=1"
        ).fetchone()
    assert identity == tuple(
        str(value)
        for value in (
            db.stat().st_dev,
            db.stat().st_ino,
            lock.stat().st_dev,
            lock.stat().st_ino,
        )
    )
