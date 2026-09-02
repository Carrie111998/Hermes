"""A worker process must not survive an in-place source generation swap."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest


WORKER = r'''import importlib
import json
import sys

module = importlib.import_module(sys.argv[1])
for line in sys.stdin:
    request = json.loads(line)
    try:
        result = module._codex_cloudflare_headers(**request)
        reply = {"ok": True, "result": result}
    except Exception as exc:
        reply = {"ok": False, "type": type(exc).__name__, "error": str(exc)}
    print(json.dumps(reply), flush=True)
'''


def _call_worker(process, payload):
    assert process.stdin is not None
    assert process.stdout is not None
    process.stdin.write(json.dumps(payload) + "\n")
    process.stdin.flush()
    return json.loads(process.stdout.readline())


def _start_worker(tmp_path, module_name):
    return subprocess.Popen(
        [sys.executable, "-u", "-c", WORKER, module_name],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_old_worker_rejects_new_caller_contract_until_process_restarted(tmp_path):
    module = tmp_path / "worker_contract.py"
    module.write_text(
        "def _codex_cloudflare_headers(headers):\n"
        "    return {'generation': 'old', **headers}\n",
        encoding="utf-8",
    )
    old_worker = _start_worker(tmp_path, "worker_contract")
    try:
        old_reply = _call_worker(
            old_worker,
            {"headers": {"x": "1"}, "base_url": "https://api.example.test"},
        )
        assert old_reply["ok"] is False
        assert old_reply["type"] == "TypeError"
        assert "base_url" in old_reply["error"]

        # Swap the source underneath the already-imported worker.  It keeps the
        # old callable object, proving that a same-process import test cannot
        # establish compatibility across the updater boundary.
        module.write_text(
            "def _codex_cloudflare_headers(headers, *, base_url=None):\n"
            "    return {'generation': 'new', 'base_url': base_url, **headers}\n",
            encoding="utf-8",
        )
        still_old = _call_worker(
            old_worker,
            {"headers": {"x": "1"}, "base_url": "https://api.example.test"},
        )
        assert still_old["ok"] is False
        assert still_old["type"] == "TypeError"
    finally:
        old_worker.terminate()
        old_worker.wait(timeout=5)

    new_worker = _start_worker(tmp_path, "worker_contract")
    try:
        fresh = _call_worker(
            new_worker,
            {"headers": {"x": "1"}, "base_url": "https://api.example.test"},
        )
        assert fresh == {
            "ok": True,
            "result": {
                "generation": "new",
                "base_url": "https://api.example.test",
                "x": "1",
            },
        }
    finally:
        new_worker.terminate()
        new_worker.wait(timeout=5)


def test_update_autostash_preserves_dirty_working_tree(tmp_path):
    """The new pre-mutation probe must not weaken the existing dirty-tree round trip."""
    from hermes_cli.update_cmd import (
        _restore_stashed_changes,
        _stash_local_changes_if_needed,
    )

    if subprocess.run(
        ["git", "--version"], capture_output=True, text=True, check=False
    ).returncode:
        pytest.skip("git is unavailable")

    def git(*args):
        return subprocess.run(
            ["git", *args],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True,
        )

    git("init", "-q")
    git("config", "user.email", "updater@example.test")
    git("config", "user.name", "Updater Test")
    tracked = tmp_path / "tracked.txt"
    tracked.write_text("installed\n", encoding="utf-8")
    git("add", "tracked.txt")
    git("commit", "-qm", "installed generation")
    tracked.write_text("local uncommitted work\n", encoding="utf-8")

    stash_ref = _stash_local_changes_if_needed(["git"], tmp_path)
    assert stash_ref
    assert tracked.read_text(encoding="utf-8") == "installed\n"

    assert _restore_stashed_changes(
        ["git"], tmp_path, stash_ref, prompt_user=False
    )
    assert tracked.read_text(encoding="utf-8") == "local uncommitted work\n"
    assert " M tracked.txt" in git("status", "--porcelain").stdout