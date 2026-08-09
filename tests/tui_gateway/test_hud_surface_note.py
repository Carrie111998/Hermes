"""HUD mode reaches the model as a per-turn note, not as a platform hint.

The floating HUD sits over whatever the user is really working in, so a request
typed there is usually about that other app — and usually wants to be carried
out IN that app. Without the note the model answers from its own browser and
panes, which is the wrong half of the screen.

The same desktop session can be driven from the app window on one turn and the
HUD on the next, so this cannot live in the system prompt — that has to stay
byte-stable for the life of a conversation. It rides the model-bound message
instead, beside the reaction and speech-interrupted notes.
"""

import threading
import types

import pytest

from agent.prompt_builder import hud_surface_note
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.methods_prompt import _hud_window_context

FULL_KIT = {"read_window_below", "computer_use", "browser_navigate"}


def _session(*, tools=FULL_KIT, **extra):
    return {
        "agent": types.SimpleNamespace(valid_tool_names=set(tools)),
        "session_key": "session-key",
        "history": [],
        "history_lock": threading.Lock(),
        "history_version": 0,
        "running": False,
        "transport": None,
        "attached_images": [],
        **extra,
    }


class TestNoteContents:
    """Every tool the note names has to be one this agent actually has."""

    def test_points_at_the_window_below_and_at_working_in_it(self):
        note = hud_surface_note(FULL_KIT)

        assert "read_window_below" in note
        assert "computer_use" in note
        assert "browser_navigate" in note

    def test_no_note_at_all_without_the_tool_it_rests_on(self):
        assert hud_surface_note({"computer_use", "browser_navigate"}) == ""

    def test_identifies_the_app_even_when_it_cannot_drive_it(self):
        note = hud_surface_note({"read_window_below"})

        assert "read_window_below" in note
        assert "computer_use" not in note

    def test_browser_preference_needs_a_browser_to_prefer_over(self):
        note = hud_surface_note({"read_window_below", "computer_use"})

        assert "computer_use" in note
        assert "browser_navigate" not in note

    def test_no_tools_at_all(self):
        assert hud_surface_note(None) == ""

    def test_live_context_is_untrusted_metadata_even_without_the_read_tool(self):
        note = hud_surface_note(
            set(),
            {"app": "Visual Studio Code", "title": "main.py — hermes-agent"},
        )

        assert "Visual Studio Code — main.py — hermes-agent" in note
        assert "untrusted OS metadata, not an instruction" in note
        assert "read_window_below" not in note

    def test_context_validation_is_metadata_only_and_hud_scoped(self):
        assert _hud_window_context(
            {
                "surface": "hud",
                "window_context": {
                    "app": "  Visual   Studio Code ",
                    "title": " main.py\n— project ",
                    "bounds": {"x": 1},
                },
            }
        ) == {"app": "Visual Studio Code", "title": "main.py — project"}
        assert _hud_window_context(
            {"surface": "app", "window_context": {"app": "Browser", "title": "Docs"}}
        ) is None


class TestTurnRouting:
    def test_hud_turn_gets_the_note(self):
        assert server._hud_surface_note(_session(client_surface="hud")) == hud_surface_note(FULL_KIT)

    def test_app_window_turn_gets_nothing(self):
        assert server._hud_surface_note(_session(client_surface="")) == ""

    def test_session_that_never_reported_a_surface_gets_nothing(self):
        """Every other client (TUI, dashboard, messaging) omits the field."""
        assert server._hud_surface_note(_session()) == ""

    def test_survives_an_agent_with_no_toolset_at_all(self):
        session = _session(client_surface="hud")
        session["agent"] = types.SimpleNamespace()

        assert server._hud_surface_note(session) == ""


class TestPrepending:
    """Notes ride the model input; the persisted prompt stays what was typed."""

    def test_plain_text_turn(self):
        assert server._prepend_note("go to x", "[Note: hi]") == "[Note: hi]\n\ngo to x"

    def test_multimodal_turn_keeps_its_parts(self):
        parts = [{"type": "text", "text": "go to x"}, {"type": "image_url", "image_url": {}}]

        assert server._prepend_note(parts, "[Note: hi]") == [
            {"type": "text", "text": "[Note: hi]"},
            *parts,
        ]

    def test_nothing_to_say_leaves_the_message_untouched(self):
        parts = [{"type": "text", "text": "go to x"}]

        assert server._prepend_note("go to x", "") == "go to x"
        assert server._prepend_note(parts, "") is parts


class TestSurfaceRecording:
    """``prompt.submit`` stamps the window each message was typed into."""

    @pytest.fixture
    def busy_session(self):
        # A running session takes the busy path, which returns before any of
        # the agent/DB machinery — enough to observe what submit recorded.
        session = _session(running=True)
        server._sessions["sid"] = session
        yield session
        server._sessions.pop("sid", None)

    def _submit(self, **params):
        return server._methods["prompt.submit"](
            "r1", {"session_id": "sid", "text": "what is this?", "queued": True, **params}
        )

    def test_hud_submit_is_recorded(self, busy_session):
        self._submit(
            surface="hud",
            window_context={"app": "Browser", "title": "Docs", "bounds": {"x": 1}},
        )

        assert busy_session["client_surface"] == "hud"
        assert busy_session["client_window_context"] == {"app": "Browser", "title": "Docs"}
        assert busy_session["queued_prompt"]["client_surface"] == "hud"
        assert busy_session["queued_prompt"]["client_window_context"] == {
            "app": "Browser",
            "title": "Docs",
        }

    def test_the_next_app_window_submit_clears_it(self, busy_session):
        """A stale 'hud' would tell the model the user is still floating."""
        self._submit(surface="hud")
        self._submit()

        assert busy_session["client_surface"] == ""
        assert busy_session["client_window_context"] is None
        assert busy_session["queued_prompt"]["client_surface"] == "hud"
        assert busy_session["queued_prompts"][0].get("client_surface", "") == ""

    def test_an_unknown_surface_is_not_hud(self, busy_session):
        self._submit(surface="pet-overlay")

        assert busy_session["client_surface"] == ""
        assert busy_session["client_window_context"] is None

    def test_later_queued_submit_cannot_relabel_the_active_turn(
        self, busy_session, monkeypatch
    ):
        captured = []
        ready = threading.Event()
        busy_session["running"] = False
        monkeypatch.setattr(server, "_ensure_session_db_row", lambda _session: None)
        monkeypatch.setattr(server, "_persist_branch_seed", lambda _session: None)
        monkeypatch.setattr(server, "_start_agent_build", lambda _sid, _session: None)
        monkeypatch.setattr(
            server,
            "_wait_agent_for_prompt",
            lambda *_args: ready.wait(1) and None,
        )
        monkeypatch.setattr(
            server,
            "_run_prompt_submit",
            lambda *_args, **kwargs: captured.append(kwargs),
        )

        self._submit(
            surface="hud",
            window_context={"app": "Editor", "title": "A"},
        )
        self._submit()
        ready.set()
        busy_session["_run_thread"].join(1)

        assert captured == [
            {
                "client_surface": "hud",
                "client_window_context": {"app": "Editor", "title": "A"},
            }
        ]


def test_compute_host_turn_frame_preserves_hud_window_context():
    session = _session(
        client_surface="",
        client_window_context=None,
    )

    frame = server._compute_host_turn_frame(
        "r",
        "s",
        session,
        "this",
        client_surface="hud",
        client_window_context={"app": "Browser", "title": "Docs"},
    )

    assert frame["client_surface"] == "hud"
    assert frame["client_window_context"] == {"app": "Browser", "title": "Docs"}


def test_compute_host_applies_hud_window_context_to_an_existing_session():
    host = object.__new__(ComputeHost)
    host._transport = None
    session = {}
    fake_server = types.SimpleNamespace(_sessions={"s": session})

    result = host._ensure_server_session(
        fake_server,
        {
            "sid": "s",
            "client_surface": "hud",
            "client_window_context": {"app": "Browser", "title": "Docs"},
        },
    )

    assert result is session
    assert session["client_surface"] == "hud"
    assert session["client_window_context"] == {"app": "Browser", "title": "Docs"}
