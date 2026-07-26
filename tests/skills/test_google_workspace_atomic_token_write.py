"""Regression tests for crash-safe Google Workspace OAuth token writes."""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import threading
from pathlib import Path


HELPER_PATH = (
    Path(__file__).resolve().parents[2]
    / "skills/productivity/google-workspace/scripts/_hermes_home.py"
)


def _load_helper():
    spec = importlib.util.spec_from_file_location(
        "google_workspace_home_atomic_test", HELPER_PATH
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_atomic_token_write_never_exposes_partial_json_to_concurrent_reader(tmp_path):
    helper = _load_helper()
    token_path = tmp_path / "google_token.json"
    helper.atomic_write_json(token_path, {"token": "initial"})
    errors: list[Exception] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                payload = json.loads(token_path.read_text(encoding="utf-8"))
                assert isinstance(payload.get("token"), str)
            except Exception as exc:  # pragma: no cover - reported in main thread
                errors.append(exc)
                stop.set()

    thread = threading.Thread(target=reader)
    thread.start()
    try:
        for index in range(200):
            helper.atomic_write_json(
                token_path,
                {"token": f"token-{index}", "padding": "x" * 4096},
            )
    finally:
        stop.set()
        thread.join()

    assert not errors, errors


def test_atomic_token_write_has_private_standalone_fallback(tmp_path):
    helper = _load_helper()
    setattr(helper, "_core_atomic_json_write", None)
    token_path = tmp_path / "google_token.json"

    helper.atomic_write_json(token_path, {"token": "standalone"})

    assert json.loads(token_path.read_text(encoding="utf-8")) == {
        "token": "standalone"
    }
    if os.name == "posix":
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_atomic_token_write_resolves_symlink_before_core_writer(tmp_path):
    helper = _load_helper()
    target = tmp_path / "mounted" / "google_token.json"
    target.parent.mkdir()
    target.write_text('{"token": "old"}', encoding="utf-8")
    link = tmp_path / "google_token.json"
    link.symlink_to(target)
    calls = []

    def capture_core(path, payload, *, mode):
        calls.append((path, payload, mode))

    setattr(helper, "_core_atomic_json_write", capture_core)

    helper.atomic_write_json(link, {"token": "new"})

    assert calls == [(target.resolve(), {"token": "new"}, 0o600)]
    assert link.is_symlink()
