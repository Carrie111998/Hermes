"""Tests for ``hermes record`` — browser workflow recording.

All CDP I/O is mocked or bypassed: these tests exercise the pure Python
event-processing functions (normalize_event, mask_value, selector_for,
build_recording, _handle_event) with synthetic CDP events, the recording
JSON schema shape, save/list round-trips, argparse wiring, and the /learn
recording-source recognition. No live browser anywhere.
"""

from __future__ import annotations

import argparse
import json

import pytest

from hermes_cli.record import (
    BINDING_NAME,
    RECORDER_JS,
    RECORDING_VERSION,
    _handle_event,
    build_recording,
    is_secret_placeholder,
    list_recordings,
    mask_value,
    normalize_event,
    register_cli,
    save_recording,
    selector_for,
)


# ---------------------------------------------------------------------------
# Secret masking — python-side defense in depth
# ---------------------------------------------------------------------------


class TestMaskValue:
    def test_password_input_type_is_masked(self):
        assert mask_value("hunter2", input_type="password", name="pw") == "{SECRET:pw}"

    def test_password_masking_never_contains_the_raw_value(self):
        masked = mask_value("hunter2", input_type="password", name="login-password")
        assert "hunter2" not in masked

    def test_secretish_autocomplete_is_masked(self):
        for ac in ("current-password", "new-password", "one-time-code"):
            assert mask_value("s3cr3t", autocomplete=ac, name="f") == "{SECRET:f}"

    def test_plain_text_value_passes_through(self):
        assert mask_value("alice@example.com", input_type="email", name="user") == "alice@example.com"

    def test_already_masked_placeholder_is_preserved_verbatim(self):
        # The recorder JS masks in-page; the python side must not double-wrap.
        assert mask_value("{SECRET:pw}", input_type="password", name="pw") == "{SECRET:pw}"

    def test_mask_falls_back_to_type_when_field_is_nameless(self):
        assert mask_value("x", input_type="password", name="") == "{SECRET:password}"

    def test_is_secret_placeholder(self):
        assert is_secret_placeholder("{SECRET:pw}")
        assert not is_secret_placeholder("hunter2")
        assert not is_secret_placeholder(None)


# ---------------------------------------------------------------------------
# Selector fallback
# ---------------------------------------------------------------------------


class TestSelectorFallback:
    def test_explicit_selector_wins(self):
        assert selector_for({"selector": "#login > button", "tag": "button"}) == "#login > button"

    def test_falls_back_to_tag_and_text(self):
        sel = selector_for({"selector": "", "tag": "button", "text": "Sign in"})
        assert "button" in sel and "Sign in" in sel

    def test_falls_back_to_bare_tag(self):
        assert selector_for({"tag": "input"}) == "input"

    def test_never_returns_empty(self):
        assert selector_for({}) == "*"


# ---------------------------------------------------------------------------
# Event normalization with synthetic CDP-shipped payloads
# ---------------------------------------------------------------------------


class TestNormalizeEvent:
    def test_click_event(self):
        step = normalize_event(
            {"t": 1000, "type": "click", "selector": "#buy", "tag": "button", "text": "Buy now"}
        )
        assert step == {"t": 1000.0, "type": "click", "selector": "#buy", "text": "Buy now"}

    def test_input_event_masks_password(self):
        step = normalize_event(
            {
                "t": 2000,
                "type": "input",
                "selector": "input[name=pw]",
                "inputType": "password",
                "name": "pw",
                "value": "hunter2",  # simulates a recorder that failed to mask
            }
        )
        assert step["value"] == "{SECRET:pw}"
        assert "hunter2" not in json.dumps(step)

    def test_input_event_keeps_plain_value(self):
        step = normalize_event(
            {"t": 3, "type": "input", "selector": "#user", "inputType": "text", "value": "alice"}
        )
        assert step["value"] == "alice"

    def test_enter_event(self):
        step = normalize_event({"t": 5, "type": "enter", "selector": "#pw"})
        assert step == {"t": 5.0, "type": "enter", "selector": "#pw"}

    def test_navigate_event(self):
        step = normalize_event({"t": 9, "type": "navigate", "url": "https://x.test/home"})
        assert step == {"t": 9.0, "type": "navigate", "url": "https://x.test/home"}

    def test_navigate_without_url_is_dropped(self):
        assert normalize_event({"t": 9, "type": "navigate"}) is None

    def test_manual_event(self):
        step = normalize_event({"t": 1, "type": "manual", "text": "clicked the login button"})
        assert step == {"t": 1.0, "type": "manual", "text": "clicked the login button"}

    def test_unknown_type_is_dropped(self):
        assert normalize_event({"t": 1, "type": "scroll"}) is None

    def test_non_dict_is_dropped(self):
        assert normalize_event("nope") is None
        assert normalize_event(None) is None

    def test_bad_timestamp_defaults_to_zero(self):
        assert normalize_event({"t": "junk", "type": "enter", "selector": "#a"})["t"] == 0.0


# ---------------------------------------------------------------------------
# Recording assembly — ordering, rebasing, schema shape
# ---------------------------------------------------------------------------


class TestBuildRecording:
    def _events(self):
        # Deliberately out of order; epoch-ms timestamps like the recorder ships.
        return [
            {"t": 1753500002000, "type": "input", "selector": "#user", "inputType": "text", "value": "alice"},
            {"t": 1753500001000, "type": "click", "selector": "#login", "text": "Login"},
            {"t": 1753500003000, "type": "navigate", "url": "https://x.test/home"},
            {"t": 1753500002500, "type": "bogus"},  # dropped
        ]

    def test_schema_shape(self):
        rec = build_recording("https://x.test/", "2026-07-26T00:00:00+00:00", self._events())
        assert set(rec.keys()) == {"version", "started_at", "url", "steps"}
        assert rec["version"] == RECORDING_VERSION
        assert rec["url"] == "https://x.test/"
        assert rec["started_at"] == "2026-07-26T00:00:00+00:00"
        assert isinstance(rec["steps"], list)
        for step in rec["steps"]:
            assert "t" in step and "type" in step

    def test_steps_are_ordered_by_time_and_rebased(self):
        rec = build_recording("https://x.test/", "now", self._events())
        types = [s["type"] for s in rec["steps"]]
        assert types == ["click", "input", "navigate"]
        ts = [s["t"] for s in rec["steps"]]
        assert ts[0] == 0.0
        assert ts == sorted(ts)
        # epoch-ms deltas rebased into seconds
        assert ts[1] == pytest.approx(1.0)
        assert ts[2] == pytest.approx(2.0)

    def test_unknown_events_are_dropped_not_fatal(self):
        rec = build_recording("u", "s", self._events())
        assert len(rec["steps"]) == 3

    def test_empty_events_yield_empty_steps(self):
        rec = build_recording("u", "s", [])
        assert rec["steps"] == []

    def test_recording_json_never_contains_raw_password(self):
        events = self._events() + [
            {"t": 1753500004000, "type": "input", "selector": "#pw",
             "inputType": "password", "name": "pw", "value": "raw-secret-value"},
        ]
        rec = build_recording("u", "s", events)
        assert "raw-secret-value" not in json.dumps(rec)
        assert any(s.get("value") == "{SECRET:pw}" for s in rec["steps"])


# ---------------------------------------------------------------------------
# CDP protocol message routing (synthetic CDP messages, no websocket)
# ---------------------------------------------------------------------------


class TestHandleEvent:
    def test_binding_called_payload_is_buffered(self):
        events, state = [], {}
        payload = {"t": 1, "type": "click", "selector": "#a"}
        _handle_event(
            {"method": "Runtime.bindingCalled",
             "params": {"name": BINDING_NAME, "payload": json.dumps(payload)}},
            events, state,
        )
        assert events == [payload]

    def test_other_bindings_are_ignored(self):
        events = []
        _handle_event(
            {"method": "Runtime.bindingCalled",
             "params": {"name": "someOtherBinding", "payload": "{}"}},
            events, {},
        )
        assert events == []

    def test_malformed_binding_payload_is_ignored(self):
        events = []
        _handle_event(
            {"method": "Runtime.bindingCalled",
             "params": {"name": BINDING_NAME, "payload": "{not json"}},
            events, {},
        )
        assert events == []

    def test_top_frame_navigation_becomes_navigate_event(self):
        events = []
        _handle_event(
            {"method": "Page.frameNavigated",
             "params": {"frame": {"url": "https://x.test/next"}}},
            events, {},
        )
        assert len(events) == 1
        assert events[0]["type"] == "navigate"
        assert events[0]["url"] == "https://x.test/next"

    def test_subframe_navigation_is_ignored(self):
        events = []
        _handle_event(
            {"method": "Page.frameNavigated",
             "params": {"frame": {"url": "https://ads.test/", "parentId": "F2"}}},
            events, {},
        )
        assert events == []

    def test_internal_urls_are_ignored(self):
        events = []
        for url in ("about:blank", "chrome://newtab", "devtools://x"):
            _handle_event(
                {"method": "Page.frameNavigated", "params": {"frame": {"url": url}}},
                events, {},
            )
        assert events == []

    def test_unrelated_methods_are_ignored(self):
        events = []
        _handle_event({"method": "Network.requestWillBeSent", "params": {}}, events, {})
        assert events == []


# ---------------------------------------------------------------------------
# Recorder JS contract — masking lives in the page too
# ---------------------------------------------------------------------------


class TestRecorderJS:
    def test_masks_passwords_at_capture_time(self):
        # The load-bearing property: the placeholder is built in-page and the
        # raw value is never read for secret fields.
        assert "password" in RECORDER_JS
        assert "{SECRET:" in RECORDER_JS

    def test_covers_secretish_autocomplete(self):
        for ac in ("current-password", "new-password", "one-time-code"):
            assert ac in RECORDER_JS

    def test_documented_event_types_are_emitted(self):
        for etype in ('"click"', '"input"', '"enter"'):
            assert f"type: {etype}" in RECORDER_JS

    def test_uses_the_cdp_binding_channel(self):
        assert BINDING_NAME in RECORDER_JS

    def test_captures_final_value_on_change_not_keystrokes(self):
        assert '"change"' in RECORDER_JS
        assert '"keypress"' not in RECORDER_JS


# ---------------------------------------------------------------------------
# Save / list round-trip
# ---------------------------------------------------------------------------


class TestSaveAndList:
    def test_save_and_list_roundtrip(self, tmp_path):
        rec = build_recording(
            "https://x.test/", "2026-07-26T00:00:00+00:00",
            [{"t": 1, "type": "click", "selector": "#a", "text": "Go"}],
        )
        path = save_recording(rec, "My Flow!!", recordings_dir=tmp_path)
        assert path.exists()
        assert path.name.startswith("my-flow-")
        assert json.loads(path.read_text(encoding="utf-8")) == rec

        listed = list_recordings(recordings_dir=tmp_path)
        assert len(listed) == 1
        assert listed[0]["steps"] == 1
        assert listed[0]["url"] == "https://x.test/"

    def test_list_empty_dir(self, tmp_path):
        assert list_recordings(recordings_dir=tmp_path / "nope") == []

    def test_slug_is_sanitized(self, tmp_path):
        path = save_recording(build_recording("u", "s", []), "../../etc/passwd", recordings_dir=tmp_path)
        assert path.parent == tmp_path
        assert "/" not in path.name.replace(path.suffix, "").replace("-", "")


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


class TestArgparseWiring:
    def _parser(self):
        p = argparse.ArgumentParser(prog="hermes record")
        register_cli(p)
        return p

    def test_defaults(self):
        args = self._parser().parse_args([])
        assert args.slug is None
        assert args.manual is False
        assert args.list is False
        assert callable(args.func)

    def test_flags_parse(self):
        args = self._parser().parse_args(["--slug", "checkout", "--manual"])
        assert args.slug == "checkout"
        assert args.manual is True

    def test_list_flag(self):
        assert self._parser().parse_args(["--list"]).list is True

    def test_record_is_a_known_builtin_subcommand(self):
        from hermes_cli.main import _BUILTIN_SUBCOMMANDS

        assert "record" in _BUILTIN_SUBCOMMANDS


# ---------------------------------------------------------------------------
# /learn recognizes recordings as a source
# ---------------------------------------------------------------------------


class TestLearnRecordingIntegration:
    def test_recording_path_triggers_replay_guidance(self):
        from agent.learn_prompt import build_learn_prompt

        prompt = build_learn_prompt("recording ~/.hermes/recordings/checkout-20260726.json")
        low = prompt.lower()
        # Replay guidance names the browser tools that re-drive the flow.
        for tool in ("browser_navigate", "browser_click", "browser_type", "browser_press"):
            assert tool in prompt
        # Secret placeholder guidance: ask for env/secret refs, never inline.
        assert "{SECRET:" in prompt
        assert "never" in low and "inline" in low
        assert "replay" in low

    def test_bare_recordings_json_path_is_recognized_without_keyword(self):
        from agent.learn_prompt import build_learn_prompt, _RECORDING_GUIDANCE

        prompt = build_learn_prompt("/home/me/.hermes/recordings/login-flow-20260726.json")
        assert _RECORDING_GUIDANCE in prompt

    def test_non_recording_requests_do_not_get_the_block(self):
        from agent.learn_prompt import build_learn_prompt, _RECORDING_GUIDANCE

        assert _RECORDING_GUIDANCE not in build_learn_prompt("https://api.example.com/docs")
        assert _RECORDING_GUIDANCE not in build_learn_prompt("")

    def test_recording_guidance_documents_the_schema(self):
        from agent.learn_prompt import _RECORDING_GUIDANCE

        for key in ("version", "started_at", "url", "steps"):
            assert key in _RECORDING_GUIDANCE

    def test_standards_still_travel_with_recording_prompts(self):
        from agent.learn_prompt import build_learn_prompt, _AUTHORING_STANDARDS

        prompt = build_learn_prompt("recording foo.json")
        assert _AUTHORING_STANDARDS in prompt
