"""`hermes webhook orca-*` — the launch and notify side of the Orca bridge.

The interesting piece here is :func:`_build_destination_path`. Orca's notifier
turns one configured endpoint into a DYNAMIC route by appending
``/orca/<event>``; Hermes' bridge is a single route that reads the kind from
the signed body, because the request path is not covered by the HMAC. Getting
the strip wrong sends every notification to a path the adapter does not serve.
"""

import argparse
import json
from unittest.mock import patch

import pytest

from hermes_cli import webhook as wh
from hermes_cli.webhook import _build_destination_path


def _args(**kw):
    return argparse.Namespace(**kw)


# ---------------------------------------------------------------------------
# G1 — dynamic Orca route stripping
# ---------------------------------------------------------------------------

class TestBuildDestinationPath:
    def test_exact_event_type_dynamic_route_targets_the_base_url(self):
        """Both dynamic segments go, not just the event name.

        Stripping only ``<event>`` leaves ``/webhooks/orca/orca`` — a route
        the adapter does not serve, so every notification 404s while still
        "looking stripped".
        """
        assert _build_destination_path(
            "/webhooks/orca/orca/worker_done", "worker_done"
        ) == "/webhooks/orca"

    def test_named_bridge_route_keeps_its_own_name(self):
        assert _build_destination_path(
            "/webhooks/orca-bridge/orca/worker_done", "worker_done"
        ) == "/webhooks/orca-bridge"

    @pytest.mark.parametrize(
        "event", ["worker_done", "hermes-ready", "exit", "Stop"]
    )
    def test_every_event_type_strips_to_the_same_base(self, event):
        assert _build_destination_path(
            f"/webhooks/orca/orca/{event}", event
        ) == "/webhooks/orca"

    def test_a_path_that_is_not_dynamic_is_untouched(self):
        assert _build_destination_path(
            "/webhooks/orca", "worker_done"
        ) == "/webhooks/orca"

    def test_a_mismatched_event_is_not_stripped(self):
        """The tail is only dynamic if it IS this event — never guessed."""
        assert _build_destination_path(
            "/webhooks/orca/orca/worker_done", "exit"
        ) == "/webhooks/orca/orca/worker_done"

    def test_a_route_literally_named_orca_is_not_eaten(self):
        """``/webhooks/orca`` alone must survive: there is no /orca/<event>."""
        assert _build_destination_path("/webhooks/orca", "orca") == (
            "/webhooks/orca"
        )

    def test_empty_event_never_strips(self):
        assert _build_destination_path(
            "/webhooks/orca/orca/worker_done", ""
        ) == "/webhooks/orca/orca/worker_done"

    def test_only_the_orca_segment_triggers_a_strip(self):
        """A ``/<something-else>/<event>`` tail is left exactly alone."""
        assert _build_destination_path(
            "/webhooks/gh/github/worker_done", "worker_done"
        ) == "/webhooks/gh/github/worker_done"


class TestOrcaNotifyPostsToTheBaseRoute:
    """End-to-end on the CLI side: the request must reach the base route."""

    def _capture_post(self, monkeypatch, **arg_overrides):
        sent = {}

        class _Resp:
            status = 200

            def read(self):
                return b'{"status": "observed"}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_urlopen(req, timeout=None):
            sent["url"] = req.full_url
            sent["body"] = req.data
            sent["headers"] = dict(req.headers)
            return _Resp()

        monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
        monkeypatch.setattr(
            wh, "_get_webhook_base_url", lambda: "http://127.0.0.1:8644"
        )
        monkeypatch.setattr(
            wh, "_get_webhook_config",
            lambda: {"extra": {"routes": {"orca": {"secret": "s3cret"}}}},
        )
        args = _args(run_id="run_6e33f11c3f86", event="worker_done",
                     route="orca", secret="", event_id="", sequence=-1)
        for key, value in arg_overrides.items():
            setattr(args, key, value)
        wh._cmd_orca_notify(args)
        return sent

    def test_posts_to_the_base_url_not_the_dynamic_route(self, monkeypatch):
        sent = self._capture_post(monkeypatch)
        assert sent["url"] == "http://127.0.0.1:8644/webhooks/orca"
        assert "/orca/worker_done" not in sent["url"]

    def test_event_kind_travels_in_the_signed_body(self, monkeypatch):
        """The path is unauthenticated, so the kind must ride the body."""
        sent = self._capture_post(monkeypatch, event="exit")
        assert json.loads(sent["body"])["kind"] == "exit"
        assert sent["url"].endswith("/webhooks/orca")

    def test_signature_is_the_replay_protected_v2_scheme(self, monkeypatch):
        import hashlib
        import hmac

        sent = self._capture_post(monkeypatch)
        headers = {k.lower(): v for k, v in sent["headers"].items()}
        ts = headers["X-Webhook-Timestamp".lower()]
        expected = hmac.new(
            b"s3cret", ts.encode() + b"." + sent["body"], hashlib.sha256
        ).hexdigest()
        assert headers["X-Webhook-Signature-V2".lower()] == expected
        # The body-only scheme must not be offered as an alternative.
        assert "x-webhook-signature" not in headers

    def test_missing_secret_refuses_to_send(self, monkeypatch, capsys):
        monkeypatch.setattr(wh, "_get_webhook_config", lambda: {"extra": {}})
        monkeypatch.setattr(
            wh, "_get_webhook_base_url", lambda: "http://127.0.0.1:8644"
        )
        called = []
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **k: called.append(1),
        )
        wh._cmd_orca_notify(_args(
            run_id="run_1", event="worker_done", route="orca",
            secret="", event_id="", sequence=-1,
        ))
        assert "no HMAC secret" in capsys.readouterr().out
        assert called == []


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

class TestOrcaRegister:
    def test_registers_and_reports_the_routing_target(self, capsys):
        from tools import orca_bridge

        orca_bridge.start()
        orca_bridge._reset_for_tests()
        try:
            wh._cmd_orca_register(_args(
                run_id="run_6e33f11c3f86", goal="ship it",
                session_key="agent:main:mattermost:thread:chan:root",
                worktree="/tmp/wt", terminal="",
            ))
            out = capsys.readouterr().out
            assert "run_6e33f11c3f86" in out
            assert "agent:main:mattermost:thread:chan:root" in out

            run = orca_bridge.get_run("run_6e33f11c3f86")
            assert run["goal"] == "ship it"
            assert run["session_key"] == (
                "agent:main:mattermost:thread:chan:root"
            )
            assert run["worktree"] == "/tmp/wt"
            assert run["state"] == "open"
        finally:
            orca_bridge._reset_for_tests()
            orca_bridge.stop()

    def test_invalid_run_id_is_refused_without_touching_state(self, capsys):
        with patch("tools.orca_bridge.register_run") as reg:
            wh._cmd_orca_register(_args(
                run_id="../etc/passwd", goal="", session_key="",
                worktree="", terminal="",
            ))
        assert "Invalid Orca run id" in capsys.readouterr().out
        reg.assert_not_called()

    def test_invalid_terminal_handle_is_refused(self, capsys):
        with patch("tools.orca_bridge.register_run") as reg:
            wh._cmd_orca_register(_args(
                run_id="run_6e33f11c3f86", goal="", session_key="",
                worktree="", terminal="bad handle!",
            ))
        assert "Invalid Orca terminal handle" in capsys.readouterr().out
        reg.assert_not_called()

    def test_unrouted_registration_says_so(self, capsys):
        from tools import orca_bridge

        orca_bridge.start()
        orca_bridge._reset_for_tests()
        try:
            wh._cmd_orca_register(_args(
                run_id="run_unrouted", goal="", session_key="",
                worktree="", terminal="",
            ))
            assert "will not be routed" in capsys.readouterr().out
        finally:
            orca_bridge._reset_for_tests()
            orca_bridge.stop()


class TestOrcaRunsAndSweep:
    def test_runs_listing_is_empty_by_default(self, capsys):
        with patch("tools.orca_bridge.list_runs", return_value=[]):
            wh._cmd_orca_runs(_args(state=""))
        assert "No Orca runs registered" in capsys.readouterr().out

    def test_runs_listing_shows_state_and_target(self, capsys):
        rows = [{"run_id": "run_a", "state": "open", "goal": "do it",
                 "session_key": "agent:main:mattermost:thread:c:r"}]
        with patch("tools.orca_bridge.list_runs", return_value=rows):
            wh._cmd_orca_runs(_args(state=""))
        out = capsys.readouterr().out
        assert "run_a" in out and "[open]" in out and "do it" in out

    def test_sweep_reports_the_delivered_count(self, capsys):
        with patch("tools.orca_bridge.sweep", return_value=3), \
             patch("tools.orca_bridge.start"):
            wh._cmd_orca_sweep(_args())
        assert "3 newly delivered" in capsys.readouterr().out

    def test_sweep_failure_is_a_message_not_a_traceback(self, capsys):
        with patch("tools.orca_bridge.sweep",
                   side_effect=RuntimeError("orca is down")), \
             patch("tools.orca_bridge.start"):
            wh._cmd_orca_sweep(_args())
        out = capsys.readouterr().out
        assert "could not reach Orca" in out
        assert "Traceback" not in out


class TestBackwardCompatibility:
    def test_usage_line_lists_old_and_new_subcommands(self, capsys):
        wh.webhook_command(_args(webhook_action=None))
        out = capsys.readouterr().out
        for expected in ("subscribe", "list", "remove", "test",
                         "orca-register", "orca-runs", "orca-sweep",
                         "orca-notify"):
            assert expected in out

    def test_parser_still_builds_every_subcommand(self):
        from hermes_cli.subcommands.webhook import build_webhook_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        build_webhook_parser(subparsers, cmd_webhook=lambda a: None)

        for argv, action in (
            (["webhook", "list"], "list"),
            (["webhook", "remove", "x"], "remove"),
            (["webhook", "orca-runs"], "orca-runs"),
            (["webhook", "orca-sweep"], "orca-sweep"),
            (["webhook", "orca-register", "--run-id", "run_1"],
             "orca-register"),
            (["webhook", "orca-notify", "--run-id", "run_1"], "orca-notify"),
        ):
            parsed = parser.parse_args(argv)
            assert parsed.webhook_action == action

    def test_orca_notify_defaults_are_sane(self):
        from hermes_cli.subcommands.webhook import build_webhook_parser

        parser = argparse.ArgumentParser()
        subparsers = parser.add_subparsers(dest="command")
        build_webhook_parser(subparsers, cmd_webhook=lambda a: None)
        parsed = parser.parse_args(
            ["webhook", "orca-notify", "--run-id", "run_1"]
        )
        assert parsed.event == "worker_done"
        assert parsed.route == "orca"
        assert parsed.sequence == -1
