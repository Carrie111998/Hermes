"""A live computer-use grant must be reused instead of spawned again.

Desktop's in-memory grant ledger dies on a renderer restart while the
backend process can still be running. The next Grant click would overwrite
``_ACTION_PROCS['computer-use-grant']`` and orphan the first TCC dialog
unless the endpoint coalesces the live handle.
"""

import sys

import pytest


class TestGrantComputerUseCoalesce:
    @pytest.fixture(autouse=True)
    def _setup(self, monkeypatch, _isolate_hermes_home):
        try:
            from starlette.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi/starlette not installed")

        import hermes_state
        from hermes_constants import get_hermes_home
        from hermes_cli.web_server import app, _SESSION_HEADER_NAME, _SESSION_TOKEN

        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(
            hermes_state, "DEFAULT_DB_PATH", get_hermes_home() / "state.db"
        )
        self.client = TestClient(app)
        self.client.headers[_SESSION_HEADER_NAME] = _SESSION_TOKEN

    def test_reuses_a_live_grant_without_spawning(self, monkeypatch):
        import hermes_cli.web_server as web_server

        class _LiveProc:
            pid = 4242

            def poll(self):
                return None

        spawned = []

        def _fail_spawn(*_args, **_kwargs):
            spawned.append(True)
            raise AssertionError("live grant must not spawn again")

        monkeypatch.setattr(web_server, "_spawn_hermes_action", _fail_spawn)
        monkeypatch.setitem(web_server._ACTION_PROCS, "computer-use-grant", _LiveProc())

        resp = self.client.post("/api/tools/computer-use/permissions/grant")

        assert resp.status_code == 200
        assert resp.json() == {
            "ok": True,
            "pid": 4242,
            "name": "computer-use-grant",
        }
        assert spawned == []

    def test_spawns_when_the_previous_grant_has_exited(self, monkeypatch):
        import hermes_cli.web_server as web_server

        class _DeadProc:
            pid = 11

            def poll(self):
                return 0

        class _NewProc:
            pid = 99

        monkeypatch.setitem(web_server._ACTION_PROCS, "computer-use-grant", _DeadProc())
        monkeypatch.setattr(
            web_server, "_spawn_hermes_action", lambda *_args, **_kwargs: _NewProc()
        )

        resp = self.client.post("/api/tools/computer-use/permissions/grant")

        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "pid": 99, "name": "computer-use-grant"}
