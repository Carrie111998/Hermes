"""Contracts for the terminal-palette color adapters in ``skin_engine``.

A skin may spell a color three ways: a ``#rrggbb`` literal, one of the
terminal's 16 palette slots (``ansi:red``), or ``""`` for the terminal
default. Rich, prompt_toolkit and raw SGR each name those slots differently
and none of them accepts the skin's spelling, so these tests assert the
adapters' *behavior* against the real engines — every produced value is fed
to ``rich.color.Color.parse`` / ``prompt_toolkit.styles.Style.from_dict``
rather than compared to a hardcoded string table (which would just restate
the implementation).
"""
import pytest

from hermes_cli.skin_engine import (
    get_prompt_toolkit_style_overrides,
    is_terminal_palette_color,
    to_prompt_toolkit_color,
    to_rich_color,
    to_sgr_bg,
    to_sgr_fg,
)

# The vocabulary a skin author may write. Kept here (not imported from the
# implementation) because it IS the contract: these 16 names plus "" must work.
ANSI_NAMES = [
    "black", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "blackBright", "redBright", "greenBright", "yellowBright",
    "blueBright", "magentaBright", "cyanBright", "whiteBright",
]
ANSI_VALUES = [f"ansi:{name}" for name in ANSI_NAMES]
GARBAGE = ["ansi:chartreuse", "ansi:", "lolnope", None, 123, [], "ANSI:RED"]


def _rich_parse(value):
    from rich.color import Color

    return Color.parse(value)


def _ptk_style(value):
    from prompt_toolkit.styles import Style

    # Both an fg rule and a bg fill: a value that parses as a foreground can
    # still blow up behind `bg:`.
    return Style.from_dict({"x": value, "y": f"bg:{value}"})


# ---------------------------------------------------------------------------
# T2 — every engine accepts every value in the vocabulary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ANSI_VALUES + [""])
def test_rich_accepts_every_terminal_palette_value(value):
    _rich_parse(to_rich_color(value))


@pytest.mark.parametrize("value", ANSI_VALUES + [""])
def test_prompt_toolkit_accepts_every_terminal_palette_value(value):
    _ptk_style(to_prompt_toolkit_color(value))


@pytest.mark.parametrize("value", ANSI_VALUES + [""])
def test_sgr_escapes_are_well_formed_palette_codes(value):
    fg, bg = to_sgr_fg(value), to_sgr_bg(value)

    assert fg.startswith("\033[") and fg.endswith("m")
    assert bg.startswith("\033[") and bg.endswith("m")
    fg_code, bg_code = int(fg[2:-1]), int(bg[2:-1])
    # 30-37/90-97 fg and 40-47/100-107 bg, or the 39/49 defaults. Never a
    # truecolor (38;2;…) or 256-color (38;5;…) sequence.
    assert fg_code in {39} | set(range(30, 38)) | set(range(90, 98))
    assert bg_code in {49} | set(range(40, 48)) | set(range(100, 108))


def test_bold_sgr_keeps_the_palette_code():
    plain, bold = to_sgr_fg("ansi:yellow"), to_sgr_fg("ansi:yellow", bold=True)

    assert bold == plain.replace("\033[", "\033[1;")


def test_style_overrides_still_cover_the_whole_prompt_toolkit_ui():
    # Guards against "fixing" ansi support by deleting the rules that break.
    assert len(get_prompt_toolkit_style_overrides()) >= 38


def test_every_style_override_parses(monkeypatch):
    from hermes_cli import skin_engine
    from prompt_toolkit.styles import Style

    # A skin whose every color is a palette slot — the shape D8 ships.
    skin = skin_engine.load_skin("default")
    skin.colors = {key: "ansi:cyan" for key in skin.colors}
    monkeypatch.setattr(skin_engine, "get_active_skin", lambda: skin)

    overrides = get_prompt_toolkit_style_overrides()

    assert overrides  # the fixture actually took effect
    Style.from_dict(overrides)
    for rule in overrides.values():
        assert "ansi:" not in rule


# ---------------------------------------------------------------------------
# T3 — base and bright stay distinct in every engine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("base", ANSI_NAMES[:8])
def test_base_and_bright_never_collapse(base):
    dark, light = f"ansi:{base}", f"ansi:{base}Bright"

    assert to_rich_color(dark) != to_rich_color(light)
    assert to_prompt_toolkit_color(dark) != to_prompt_toolkit_color(light)
    assert to_sgr_fg(dark) != to_sgr_fg(light)
    assert to_sgr_bg(dark) != to_sgr_bg(light)


@pytest.mark.parametrize("name", ANSI_NAMES)
def test_each_slot_is_unique_per_engine(name):
    # prompt_toolkit names slot 7 `ansigray` and slot 15 `ansiwhite`; a naive
    # "ansi" + name mapping raises on one and aliases the other.
    others = [n for n in ANSI_NAMES if n != name]

    assert to_prompt_toolkit_color(f"ansi:{name}") not in {
        to_prompt_toolkit_color(f"ansi:{o}") for o in others
    }
    assert to_rich_color(f"ansi:{name}") not in {to_rich_color(f"ansi:{o}") for o in others}


# ---------------------------------------------------------------------------
# Identity: a hex skin must come out byte-identical
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ["#FFD700", "#0e0e12", "#fff8dc", "cyan", "bright_white"])
def test_non_palette_values_pass_through_unchanged(value):
    assert to_rich_color(value) == value
    assert to_prompt_toolkit_color(value) == value


def test_empty_string_means_default_in_rich_and_inherit_in_prompt_toolkit():
    # rich raises on ""; prompt_toolkit reads it as "inherit", which is the
    # deliberate default for typed input — translating it would change the
    # cascade.
    assert to_rich_color("") == "default"
    assert to_prompt_toolkit_color("") == ""
    with pytest.raises(Exception):
        _rich_parse("")


# ---------------------------------------------------------------------------
# T5 — a typo in a user's skin degrades, never raises
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", GARBAGE)
def test_adapters_never_raise_on_unknown_input(value):
    to_rich_color(value)
    to_prompt_toolkit_color(value)
    to_sgr_fg(value)
    to_sgr_bg(value)


@pytest.mark.parametrize("value", ["ansi:chartreuse", "ansi:", None, 123, []])
def test_malformed_palette_slots_still_produce_parseable_output(value):
    # A typo inside the vocabulary this module owns must not reach an engine
    # as garbage — it becomes the terminal default.
    _rich_parse(to_rich_color(value))
    _ptk_style(to_prompt_toolkit_color(value))


@pytest.mark.parametrize("value", ["lolnope", "ANSI:RED"])
def test_values_outside_the_vocabulary_are_left_verbatim(value):
    # Not this module's vocabulary (the `ansi:` prefix is case-sensitive, like
    # the TUI's), so it passes through for the engine to judge — exactly as it
    # did before terminal colors existed. Rejecting it here would silently eat
    # rich's own named colors.
    assert to_rich_color(value) == value
    assert to_prompt_toolkit_color(value) == value


@pytest.mark.parametrize("value", ["ansi:chartreuse", "ansi:", None, 123])
def test_unknown_palette_slots_degrade_to_the_terminal_default(value):
    assert to_rich_color(value) == "default"
    assert to_prompt_toolkit_color(value) == ""
    assert to_sgr_fg(value) == "\033[39m"
    assert to_sgr_bg(value) == "\033[49m"


# ---------------------------------------------------------------------------
# Classification: callers use this to decide whether to keep their hex path
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ANSI_VALUES + ["", "ansi:chartreuse", "ansi:"])
def test_terminal_palette_values_are_classified_as_such(value):
    assert is_terminal_palette_color(value) is True


@pytest.mark.parametrize("value", ["#FFD700", "cyan", "lolnope", None, 123])
def test_literal_and_junk_values_are_left_to_the_caller(value):
    assert is_terminal_palette_color(value) is False


# ---------------------------------------------------------------------------
# Downstream consumers: the CLI's own SGR/rich builders honor the vocabulary
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("value", ANSI_VALUES + [""])
def test_cli_hex_to_ansi_emits_palette_codes_not_truecolor(value):
    from cli import _hex_to_ansi

    assert "38;2;" not in _hex_to_ansi(value)
    assert "38;5;" not in _hex_to_ansi(value)
    assert _hex_to_ansi(value) == to_sgr_fg(value)
    assert _hex_to_ansi(value, bold=True) == to_sgr_fg(value, bold=True)


@pytest.mark.parametrize("value", ["#268bd2", "#FFD700"])
def test_cli_hex_to_ansi_keeps_the_hex_path_intact(value):
    from cli import _hex_to_ansi, _maybe_remap_for_light_mode

    r, g, b = (int(_maybe_remap_for_light_mode(value)[i:i + 2], 16) for i in (1, 3, 5))
    assert _hex_to_ansi(value) == f"\033[38;2;{r};{g};{b}m"
    assert _hex_to_ansi(value, bold=True) == f"\033[1;38;2;{r};{g};{b}m"


def test_diff_colors_follow_a_terminal_palette_skin(monkeypatch):
    from agent import display
    from hermes_cli import skin_engine

    skin = skin_engine.load_skin("default")
    skin.colors = dict(skin.colors, banner_dim="ansi:blackBright", session_label="ansi:blueBright")
    monkeypatch.setattr(skin_engine, "get_active_skin", lambda: skin)
    monkeypatch.setattr(display, "_diff_colors_cached", None, raising=False)

    colors = display._diff_ansi()

    assert colors["dim"] == to_sgr_fg("ansi:blackBright")
    assert colors["file"] == to_sgr_fg("ansi:blueBright")
