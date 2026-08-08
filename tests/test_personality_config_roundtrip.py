"""Personality selection must round-trip: survive a restart AND leave the
user's manual ``agent.system_prompt`` intact.

Regression coverage for the cluster:
  * #81791 — selecting a personality clobbered the manual agent.system_prompt
  * #51882 — Desktop Settings wrote only display.personality, overlay ignored
  * #58774 / #51155 — personality selection did not persist across sessions

The invariant under test is the *round trip*, not any single write:

    select personality -> persist -> restart -> personality still active
                                             -> manual prompt still intact

Both halves are asserted together on purpose. Fixing either one alone
reintroduces the other (that is exactly how this cluster arose), so a test
that checks only one half would go green on a half-fix.
"""

import pytest

from hermes_cli.config import (
    render_personality_prompt,
    resolve_personality_overlay,
)


PERSONALITIES = {
    "pirate": "You are a pirate. Arr.",
    "coder": {"system_prompt": "You write code.", "tone": "terse", "style": "examples"},
    "listy": ["Line one.", "Line two."],
}


def _cfg(manual="", personality="", personalities=None):
    return {
        "agent": {
            "system_prompt": manual,
            "personalities": PERSONALITIES if personalities is None else personalities,
        },
        "display": {"personality": personality},
    }


# --------------------------------------------------------------------------
# render_personality_prompt — one canonical renderer for every call site
# --------------------------------------------------------------------------

def test_render_plain_string():
    assert render_personality_prompt("You are a pirate.") == "You are a pirate."


def test_render_dict_composes_tone_and_style():
    out = render_personality_prompt(PERSONALITIES["coder"])
    assert out == "You write code.\nTone: terse\nStyle: examples"


def test_render_dict_omits_absent_optional_fields():
    assert render_personality_prompt({"system_prompt": "Just this."}) == "Just this."


def test_render_list_joins_lines():
    assert render_personality_prompt(["Line one.", "Line two."]) == "Line one.\nLine two."


def test_render_none_is_empty():
    assert render_personality_prompt(None) == ""


# --------------------------------------------------------------------------
# resolve_personality_overlay — the startup read side
# --------------------------------------------------------------------------

def test_personality_alone_resolves_to_its_prompt():
    """#58774/#51155: a saved selection must actually apply on next start."""
    assert resolve_personality_overlay(_cfg(personality="pirate")) == "You are a pirate. Arr."


def test_manual_prompt_alone_is_returned_unchanged():
    assert resolve_personality_overlay(_cfg(manual="Be concise.")) == "Be concise."


def test_manual_and_personality_compose_manual_first():
    """#81791: the manual prompt is preserved, not replaced."""
    out = resolve_personality_overlay(_cfg(manual="Be concise.", personality="pirate"))
    assert out == "Be concise.\n\nYou are a pirate. Arr."


def test_neutral_names_disable_the_overlay():
    for name in ("", "none", "default", "neutral", "NONE", "  Neutral  "):
        out = resolve_personality_overlay(_cfg(manual="Be concise.", personality=name))
        assert out == "Be concise.", f"{name!r} should clear the overlay"


def test_personality_name_is_case_insensitive():
    assert resolve_personality_overlay(_cfg(personality="PiRaTe")) == "You are a pirate. Arr."


def test_unknown_personality_falls_back_to_manual_prompt():
    out = resolve_personality_overlay(_cfg(manual="Be concise.", personality="nope"))
    assert out == "Be concise."


def test_legacy_personality_copy_is_not_doubled():
    """Configs written by older builds hold a personality's text in
    agent.system_prompt. That copy is legacy cache, not a manual prompt —
    it must not be concatenated in front of the same personality."""
    stale = PERSONALITIES["pirate"]
    out = resolve_personality_overlay(_cfg(manual=stale, personality="pirate"))
    assert out == "You are a pirate. Arr."
    assert out.count("Arr.") == 1


def test_legacy_copy_of_a_different_personality_is_dropped():
    """display.personality is authoritative; a stale copy of some *other*
    personality's text is not the user's manual prompt."""
    out = resolve_personality_overlay(
        _cfg(manual=PERSONALITIES["pirate"], personality="coder")
    )
    assert out == "You write code.\nTone: terse\nStyle: examples"
    assert "pirate" not in out.lower()


def test_dict_personality_resolves_through_the_overlay():
    out = resolve_personality_overlay(_cfg(personality="coder"))
    assert out == "You write code.\nTone: terse\nStyle: examples"


def test_missing_and_malformed_config_sections_are_tolerated():
    assert resolve_personality_overlay(None) == ""
    assert resolve_personality_overlay({}) == ""
    assert resolve_personality_overlay({"agent": "not-a-dict"}) == ""
    assert resolve_personality_overlay({"display": {"personality": "pirate"}}) == ""
    assert (
        resolve_personality_overlay(
            {"agent": {"system_prompt": "M", "personalities": None},
             "display": {"personality": "pirate"}}
        )
        == "M"
    )


# --------------------------------------------------------------------------
# The round trip, per write path
# --------------------------------------------------------------------------

def _select_via_tui_gateway(cfg, name):
    """Drive the real tui_gateway config.set personality write semantics."""
    import tui_gateway.server as server

    # _available_personalities() intentionally prefers the real loaded config
    # over the dict passed in, so pin it to this test's fixture.
    original = server._available_personalities
    server._available_personalities = lambda c=None: (
        (cfg.get("agent") or {}).get("personalities") or {}
    )
    try:
        pname, _prompt = server._validate_personality(name, cfg)
    finally:
        server._available_personalities = original
    cfg.setdefault("display", {})["personality"] = pname
    return cfg


def _select_via_gateway_slash(cfg, name):
    """Mirror gateway/slash_commands.py /personality persistence."""
    cfg.setdefault("display", {})["personality"] = (
        "" if name in {"none", "default", "neutral"} else name
    )
    return cfg


def _select_via_web_put(cfg, name):
    """Drive the real dashboard PUT /api/config validation + normalization."""
    from hermes_cli.web_server import _validate_web_personality

    incoming = {"display": {"personality": name}}
    _validate_web_personality(incoming, cfg)
    cfg.setdefault("display", {})["personality"] = incoming["display"]["personality"]
    return cfg


ALL_WRITE_PATHS = {
    "tui_gateway_rpc": _select_via_tui_gateway,
    "gateway_slash": _select_via_gateway_slash,
    "web_put_api_config": _select_via_web_put,
}


@pytest.mark.parametrize("path_name", sorted(ALL_WRITE_PATHS))
def test_round_trip_preserves_manual_prompt_and_personality(path_name):
    """THE bug-class test. Every write path, both invariants, after restart."""
    select = ALL_WRITE_PATHS[path_name]
    cfg = _cfg(manual="Be concise.")

    cfg = select(cfg, "pirate")

    # "Restart": resolve from the persisted config exactly as startup does.
    overlay = resolve_personality_overlay(cfg)

    assert cfg["agent"]["system_prompt"] == "Be concise.", (
        f"{path_name}: manual agent.system_prompt was clobbered (#81791)"
    )
    assert "You are a pirate. Arr." in overlay, (
        f"{path_name}: personality did not survive the restart (#58774/#51155)"
    )
    assert "Be concise." in overlay, (
        f"{path_name}: manual prompt dropped out of the resolved overlay"
    )


@pytest.mark.parametrize("path_name", sorted(ALL_WRITE_PATHS))
def test_round_trip_switching_leaves_no_stale_text(path_name):
    """Switching personalities must not accumulate or strand prior text."""
    select = ALL_WRITE_PATHS[path_name]
    cfg = _cfg(manual="Be concise.")

    cfg = select(cfg, "pirate")
    cfg = select(cfg, "coder")
    overlay = resolve_personality_overlay(cfg)

    assert cfg["agent"]["system_prompt"] == "Be concise."
    assert "You write code." in overlay
    assert "pirate" not in overlay.lower(), f"{path_name}: stale personality text"


@pytest.mark.parametrize("path_name", sorted(ALL_WRITE_PATHS))
def test_round_trip_clearing_restores_bare_manual_prompt(path_name):
    select = ALL_WRITE_PATHS[path_name]
    cfg = _cfg(manual="Be concise.")

    cfg = select(cfg, "pirate")
    cfg = select(cfg, "none")

    assert resolve_personality_overlay(cfg) == "Be concise."
    assert cfg["agent"]["system_prompt"] == "Be concise."


def test_round_trip_without_manual_prompt_is_personality_only():
    cfg = _select_via_tui_gateway(_cfg(manual=""), "pirate")
    assert resolve_personality_overlay(cfg) == "You are a pirate. Arr."


def test_web_put_rejects_unknown_personality():
    """#51906's one genuinely better behavior, kept."""
    from fastapi import HTTPException
    from hermes_cli.web_server import _validate_web_personality

    with pytest.raises(HTTPException) as exc:
        _validate_web_personality({"display": {"personality": "nope"}}, _cfg())
    assert exc.value.status_code == 400


def test_web_put_without_personality_key_is_untouched():
    from hermes_cli.web_server import _validate_web_personality

    incoming = {"agent": {"model": "x"}}
    _validate_web_personality(incoming, _cfg())
    assert incoming == {"agent": {"model": "x"}}


def test_tui_gateway_session_overlay_matches_a_restart(tmp_path, monkeypatch):
    """The live session's ephemeral prompt must equal what a restart resolves,
    or the assistant silently changes behavior when the app is reopened."""
    import tui_gateway.server as server

    cfg = _cfg(manual="Be concise.")
    monkeypatch.setattr(server, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(server, "_available_personalities", lambda c=None: PERSONALITIES)

    pname, prompt = server._validate_personality("pirate", cfg)
    live_overlay = server._composed_personality_overlay(pname, prompt)

    persisted = _select_via_tui_gateway(dict(cfg), "pirate")
    assert live_overlay == resolve_personality_overlay(persisted)
    assert live_overlay == "Be concise.\n\nYou are a pirate. Arr."


# --------------------------------------------------------------------------
# CLI /personality — the fourth write path
# --------------------------------------------------------------------------

class _FakeCLI:
    """Minimal stand-in exposing what _handle_personality_command touches."""

    from hermes_cli.cli_commands_mixin import CLICommandsMixin

    _handle_personality_command = CLICommandsMixin._handle_personality_command
    _resolve_personality_prompt = staticmethod(render_personality_prompt)

    def __init__(self):
        self.personalities = dict(PERSONALITIES)
        self.system_prompt = ""
        self.agent = object()


@pytest.fixture
def cli_with_manual_prompt(monkeypatch):
    """A CLI whose on-disk config carries a manual prompt, with the config
    writes captured instead of hitting the filesystem."""
    import hermes_cli.config as config_mod

    disk = _cfg(manual="Be concise.")
    saved = []

    monkeypatch.setattr(config_mod, "read_raw_config", lambda: disk)

    import cli as cli_mod

    def _save(key, value):
        saved.append((key, value))
        node = disk
        parts = key.split(".")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = value
        return True

    monkeypatch.setattr(cli_mod, "save_config_value", _save)
    return _FakeCLI(), disk, saved


def test_cli_personality_preserves_manual_prompt(cli_with_manual_prompt):
    cli, disk, saved = cli_with_manual_prompt

    cli._handle_personality_command("/personality pirate")

    assert disk["agent"]["system_prompt"] == "Be concise.", "#81791: manual prompt clobbered"
    assert ("display.personality", "pirate") in saved
    assert all(key != "agent.system_prompt" for key, _ in saved)
    assert cli.system_prompt == "Be concise.\n\nYou are a pirate. Arr."


def test_cli_personality_survives_restart(cli_with_manual_prompt):
    cli, disk, _saved = cli_with_manual_prompt

    cli._handle_personality_command("/personality pirate")

    # "Restart": a fresh process resolves from the persisted config.
    assert resolve_personality_overlay(disk) == cli.system_prompt


def test_cli_personality_none_restores_manual_prompt(cli_with_manual_prompt):
    cli, disk, _saved = cli_with_manual_prompt

    cli._handle_personality_command("/personality pirate")
    cli._handle_personality_command("/personality none")

    assert cli.system_prompt == "Be concise."
    assert disk["agent"]["system_prompt"] == "Be concise."
    assert resolve_personality_overlay(disk) == "Be concise."
