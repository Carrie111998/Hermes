"""Hardening tests for /api/fs/write-text staging behaviour.

Regression for #84752: the write staged to a PREDICTABLE temp name
(".{name}.hermes-tmp-{pid}"), so a local attacker (or a compromised
sibling process on the same host) could pre-create a symlink at that
path and redirect the staged write to an arbitrary file.
"""

import os

import pytest


def _client():
    try:
        from starlette.testclient import TestClient
    except ImportError:
        pytest.skip("fastapi/starlette not installed")
    import hermes_state
    from hermes_constants import get_hermes_home
    from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

    client = TestClient(app)
    client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN
    hermes_state.DEFAULT_DB_PATH = get_hermes_home() / "state.db"
    return client


class TestFsWriteTextStaging:
    @pytest.fixture(autouse=True)
    def _setup(self, _isolate_hermes_home):
        self.client = _client()

    def test_predictable_temp_symlink_not_followed(self, tmp_path):
        target = tmp_path / "target.txt"
        victim = tmp_path / "victim.txt"
        victim.write_text("VICTIM", encoding="utf-8")

        # Pre-create a symlink at the OLD predictable temp path. With the
        # mkstemp fix the staged file has a random name, so this symlink is
        # never followed and the victim stays intact.
        old_tmp = target.with_name(f".{target.name}.hermes-tmp-{os.getpid()}")
        try:
            old_tmp.symlink_to(victim)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this host/filesystem")

        resp = self.client.post(
            "/api/fs/write-text",
            json={"path": str(target), "content": "NEW"},
        )
        assert resp.status_code == 200, resp.text
        assert target.read_text(encoding="utf-8") == "NEW"
        assert victim.read_text(encoding="utf-8") == "VICTIM", (
            "staged write followed the predictable symlink and overwrote the victim"
        )

    def test_placeholder_file_at_old_temp_path_untouched(self, tmp_path):
        # Same class of bug without symlink privileges (works on Windows):
        # with the old predictable temp name, write_text('w') truncated a
        # pre-existing file at that path and os.replace consumed it. With
        # mkstemp the staged file has a random name, so the placeholder at
        # the old path is never opened.
        target = tmp_path / "target.txt"
        placeholder = target.with_name(f".{target.name}.hermes-tmp-{os.getpid()}")
        placeholder.write_text("PLACEHOLDER", encoding="utf-8")

        resp = self.client.post(
            "/api/fs/write-text",
            json={"path": str(target), "content": "NEW"},
        )
        assert resp.status_code == 200, resp.text
        assert target.read_text(encoding="utf-8") == "NEW"
        assert placeholder.read_text(encoding="utf-8") == "PLACEHOLDER", (
            "staged write truncated/consumed a file at the predictable temp path"
        )

    def test_write_is_atomic_and_leaves_no_staging_files(self, tmp_path):
        target = tmp_path / "target.txt"
        resp = self.client.post(
            "/api/fs/write-text",
            json={"path": str(target), "content": "hello"},
        )
        assert resp.status_code == 200, resp.text
        assert target.read_text(encoding="utf-8") == "hello"
        leftovers = [p.name for p in tmp_path.iterdir() if ".hermes-tmp-" in p.name]
        assert leftovers == []
