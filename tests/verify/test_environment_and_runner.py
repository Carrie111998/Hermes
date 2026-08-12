"""Tests for the verify environment manifest and the smoke runner."""

import http.server
import json
import socket
import subprocess
import sys
import threading
import time

import pytest

from agent.verify.environment import (
    load_manifest,
    load_or_detect,
    manifest_path,
    save_manifest,
)
from agent.verify.recipes import Recipe
from agent.verify.runner import run_verify


class TestManifest:
    def test_roundtrip(self, tmp_path):
        recipe = Recipe(
            name="Next.js",
            kind="nextjs",
            bootstrap=["npm install"],
            build=["npm run build"],
            test=["npm test"],
            start="npm run dev",
            port=3000,
            readiness_path="/health",
        )
        path = save_manifest(tmp_path, recipe)
        assert path == manifest_path(tmp_path)
        payload = json.loads(path.read_text())
        assert payload["version"] == 1
        assert "updatedAt" in payload
        assert load_manifest(tmp_path) == recipe

    def test_missing_file(self, tmp_path):
        assert load_manifest(tmp_path) is None

    def test_malformed_json_tolerated(self, tmp_path):
        path = manifest_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("{oops", encoding="utf-8")
        assert load_manifest(tmp_path) is None

    def test_non_dict_tolerated(self, tmp_path):
        path = manifest_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text("[1, 2, 3]", encoding="utf-8")
        assert load_manifest(tmp_path) is None

    def test_bare_recipe_shape_accepted(self, tmp_path):
        path = manifest_path(tmp_path)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"name": "Custom", "test": ["true"]}), encoding="utf-8")
        recipe = load_manifest(tmp_path)
        assert recipe.name == "Custom"
        assert recipe.test == ["true"]

    def test_manifest_wins_over_detection(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        save_manifest(tmp_path, Recipe(name="Custom", kind="custom", test=["true"]))
        recipe, source = load_or_detect(tmp_path)
        assert source == "manifest"
        assert recipe.name == "Custom"

    def test_detection_fallback(self, tmp_path):
        (tmp_path / "go.mod").write_text("module x\n", encoding="utf-8")
        recipe, source = load_or_detect(tmp_path)
        assert source == "detected"
        assert recipe.kind == "go"


class TestRunner:
    def test_all_phases_pass(self, tmp_path):
        recipe = Recipe(name="x", bootstrap=["true"], build=["true"], test=["true"])
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert result.ok
        assert [p.phase for p in result.phases] == ["bootstrap", "build", "test"]
        assert all(p.exit_code == 0 for p in result.phases)
        assert all(p.duration >= 0 for p in result.phases)

    def test_failure_stops_pipeline(self, tmp_path):
        recipe = Recipe(name="x", build=["false"], test=["true"])
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert not result.ok
        assert len(result.phases) == 1
        assert result.phases[0].exit_code == 1

    def test_output_captured(self, tmp_path):
        recipe = Recipe(name="x", test=["echo hello-verify"])
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert "hello-verify" in result.phases[0].output_tail

    def test_phase_selection(self, tmp_path):
        recipe = Recipe(name="x", bootstrap=["true"], build=["true"], test=["true"])
        result = run_verify(tmp_path, recipe, phases=("test",))
        assert [p.phase for p in result.phases] == ["test"]

    def test_phase_timeout(self, tmp_path):
        recipe = Recipe(name="x", test=["sleep 5"])
        result = run_verify(tmp_path, recipe, phase_timeout=0.3, skip_start=True)
        assert not result.ok
        assert result.phases[0].timed_out
        assert result.phases[0].exit_code is None

    def test_commands_run_in_project_root(self, tmp_path):
        (tmp_path / "marker.txt").write_text("here", encoding="utf-8")
        recipe = Recipe(name="x", test=["cat marker.txt"])
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert result.ok

    def test_result_to_dict(self, tmp_path):
        recipe = Recipe(name="x", test=["true"])
        payload = run_verify(tmp_path, recipe, skip_start=True).to_dict()
        assert payload["ok"] is True
        assert payload["recipe"] == "x"
        assert payload["phases"][0]["command"] == "true"
        assert payload["readiness"] is None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestReadiness:
    def test_verbose_start_output_does_not_block_readiness_or_teardown(self, tmp_path):
        port = _free_port()
        script = tmp_path / "verbose_server.py"
        script.write_text(
            "import http.server\n"
            "print('x' * 1_000_000, flush=True)\n"
            f"http.server.HTTPServer(('127.0.0.1', {port}), http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )
        command = subprocess.list2cmdline([sys.executable, str(script)])
        recipe = Recipe(name="verbose", start=command, port=port)

        result = run_verify(tmp_path, recipe, phases=("start",), ready_timeout=15)

        assert result.ok
        assert result.readiness is not None
        assert "x" * 1000 in result.readiness.output_tail
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    time.sleep(0.05)
            except OSError:
                break
        else:
            raise AssertionError("start process tree still owns the readiness port")

    @pytest.mark.windows_only
    def test_exited_launcher_descendant_is_terminated(self, tmp_path):
        port = _free_port()
        server = tmp_path / "server.py"
        server.write_text(
            "import http.server, sys\n"
            "http.server.HTTPServer(('127.0.0.1', int(sys.argv[1])), "
            "http.server.SimpleHTTPRequestHandler).serve_forever()\n",
            encoding="utf-8",
        )
        launcher = tmp_path / "launcher.py"
        launcher.write_text(
            "import subprocess, sys\n"
            "subprocess.Popen([sys.executable, sys.argv[1], sys.argv[2]])\n",
            encoding="utf-8",
        )
        command = subprocess.list2cmdline([sys.executable, str(launcher), str(server), str(port)])

        result = run_verify(
            tmp_path,
            Recipe(name="exited-launcher", start=command, port=port),
            phases=("start",),
            ready_timeout=15,
        )

        assert result.ok
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                    time.sleep(0.05)
            except OSError:
                break
        else:
            raise AssertionError("descendant outlived its exited launcher")

    def test_readiness_against_live_server(self, tmp_path):
        port = _free_port()
        recipe = Recipe(
            name="x",
            start=subprocess.list2cmdline(
                [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"]
            ),
            port=port,
        )
        result = run_verify(tmp_path, recipe, phases=("start",), ready_timeout=15)
        assert result.readiness is not None
        assert result.readiness.ready
        assert result.readiness.status_code == 200
        assert result.readiness.url == f"http://127.0.0.1:{port}/"
        assert result.ok

    def test_readiness_timeout_when_nothing_listens(self, tmp_path):
        port = _free_port()
        recipe = Recipe(name="x", start="sleep 30", port=port)
        result = run_verify(tmp_path, recipe, phases=("start",), ready_timeout=1.5)
        assert result.readiness is not None
        assert not result.readiness.ready
        assert not result.ok

    def test_skip_start(self, tmp_path):
        recipe = Recipe(name="x", test=["true"], start="sleep 30", port=1)
        result = run_verify(tmp_path, recipe, skip_start=True)
        assert result.readiness is None
        assert result.ok

    def test_start_skipped_after_phase_failure(self, tmp_path):
        recipe = Recipe(name="x", test=["false"], start="sleep 30", port=1)
        result = run_verify(tmp_path, recipe, stop_on_failure=False)
        assert result.readiness is None
        assert not result.ok

    def test_port_override(self, tmp_path):
        port = _free_port()

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(204)
                self.end_headers()

            def log_message(self, *a):
                pass

        server = http.server.HTTPServer(("127.0.0.1", port), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.05)
        try:
            recipe = Recipe(name="x", start="sleep 30", port=1)
            result = run_verify(
                tmp_path, recipe, phases=("start",), ready_timeout=10, port_override=port
            )
            assert result.readiness.ready
            assert result.readiness.status_code == 204
        finally:
            server.shutdown()
            thread.join(timeout=5)
