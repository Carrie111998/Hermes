"""Tests for optional-skills/creative/busy-terminal/scripts/busy_terminal.py"""

import random
import re
import sys
from pathlib import Path
from unittest import mock

import pytest

# Add the scripts dir so we can import the module directly
SCRIPTS_DIR = (
    Path(__file__).resolve().parents[2]
    / "optional-skills"
    / "creative"
    / "busy-terminal"
    / "scripts"
)
sys.path.insert(0, str(SCRIPTS_DIR))

import busy_terminal

ANSI = re.compile(r"\033\[[0-9;?]*[A-Za-z]")


class Recorder:
    """Captures what a scene paints and how long it asked to sleep."""

    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.slept = 0.0

    def write(self, text: str) -> None:
        self.chunks.append(text)

    def sleep(self, seconds: float) -> None:
        assert seconds >= 0, "a scene must never ask for a negative sleep"
        self.slept += seconds

    @property
    def text(self) -> str:
        return "".join(self.chunks)


def make_console(color: bool = False, speed: float = 1.0) -> tuple[busy_terminal.Console, Recorder]:
    rec = Recorder()
    console = busy_terminal.Console(
        width=100, height=30, color=color, speed=speed, write=rec.write, sleep=rec.sleep
    )
    return console, rec


def ticking_clock(step: float = 1.0):
    """A monotonic clock that advances a fixed step per call."""
    state = {"t": 0.0}

    def now() -> float:
        state["t"] += step
        return state["t"] - step

    return now


# ── Pure formatters ──────────────────────────────────────────────────────────


class TestProgressBar:
    def test_width_is_fixed_regardless_of_progress(self):
        widths = {len(busy_terminal.progress_bar(done, 10, width=20)) for done in range(0, 11)}
        assert widths == {20}

    def test_endpoints_are_empty_and_full(self):
        assert busy_terminal.progress_bar(0, 10, width=8) == "░" * 8
        assert busy_terminal.progress_bar(10, 10, width=8) == "█" * 8

    def test_out_of_range_clamps_instead_of_overflowing(self):
        assert busy_terminal.progress_bar(-5, 10, width=8) == "░" * 8
        assert busy_terminal.progress_bar(50, 10, width=8) == "█" * 8

    def test_zero_total_does_not_divide_by_zero(self):
        assert len(busy_terminal.progress_bar(3, 0, width=6)) == 6

    def test_fill_never_shrinks_as_progress_grows(self):
        fills = [busy_terminal.progress_bar(d, 20, width=12).count("█") for d in range(21)]
        assert fills == sorted(fills)


class TestHumanBytes:
    @pytest.mark.parametrize(
        "count,expected_unit",
        [(512, "B"), (2048, "KiB"), (5 * 1024**2, "MiB"), (3 * 1024**3, "GiB")],
    )
    def test_unit_matches_magnitude(self, count, expected_unit):
        assert busy_terminal.human_bytes(count).endswith(expected_unit)

    def test_larger_counts_never_read_as_smaller_units(self):
        assert busy_terminal.human_bytes(1024**4).endswith("GiB")


class TestNextScene:
    def test_never_repeats_the_scene_that_just_played(self):
        rng = random.Random(0)
        last = "code"
        for _ in range(200):
            chosen = busy_terminal.next_scene(rng, last)
            assert chosen != last
            last = chosen

    def test_always_returns_a_known_scene(self):
        rng = random.Random(1)
        assert {busy_terminal.next_scene(rng, "build") for _ in range(50)} <= set(
            busy_terminal.SCENES
        )

    def test_single_scene_catalog_yields_rather_than_looping(self):
        rng = random.Random(2)
        assert busy_terminal.next_scene(rng, "only", scenes=("only",)) == "only"

    def test_no_prior_scene_can_pick_any_of_them(self):
        rng = random.Random(3)
        seen = {busy_terminal.next_scene(rng, "") for _ in range(200)}
        assert seen == set(busy_terminal.SCENES)


class TestTestSummary:
    def test_failures_lead_the_line(self):
        assert busy_terminal.test_summary(10, 2, 0, 1.0).startswith("2 failed")

    def test_clean_run_omits_failed_and_skipped(self):
        line = busy_terminal.test_summary(10, 0, 0, 1.5)
        assert "failed" not in line and "skipped" not in line

    def test_always_reports_passed_and_duration(self):
        line = busy_terminal.test_summary(7, 1, 3, 2.25)
        assert "7 passed" in line and "3 skipped" in line and "2.25s" in line


# ── Highlighting ─────────────────────────────────────────────────────────────


class TestHighlight:
    def test_is_a_no_op_without_color(self):
        line = 'def go(x):  # note'
        assert busy_terminal.highlight(line, "python", color=False) == line

    def test_stripping_escapes_recovers_the_original_line(self):
        line = 'return "ok"  # 42'
        painted = busy_terminal.highlight(line, "python", color=True)
        assert ANSI.sub("", painted) == line

    def test_every_opened_color_is_closed(self):
        painted = busy_terminal.highlight('async def f(): return "x"  # 1', "python", color=True)
        assert painted.count(busy_terminal.RESET) == len(re.findall(r"\033\[38;5;\d+m", painted))

    def test_keywords_are_tinted_and_bare_identifiers_are_not(self):
        painted = busy_terminal.highlight("import widget", "python", color=True)
        assert busy_terminal.MAGENTA + "import" in painted
        assert busy_terminal.MAGENTA + "widget" not in painted

    def test_unknown_language_still_returns_the_line_intact(self):
        line = "some :: unknown ++ syntax"
        assert ANSI.sub("", busy_terminal.highlight(line, "cobol", color=True)) == line


class TestTypeOut:
    def test_repaint_redraws_the_prefix_so_the_gutter_survives(self):
        """The repaint returns to column 0 and must not land on the line numbers."""
        console, rec = make_console(color=True)
        busy_terminal.type_out(
            console, "return None", prefix="  12 │ ", language="python", rng=random.Random(1)
        )
        repaint = rec.text.split("\r")[-1]
        assert repaint.startswith("  12 │ ")

    def test_the_typed_characters_spell_the_whole_line(self):
        console, rec = make_console(color=False)
        busy_terminal.type_out(console, "import asyncio", rng=random.Random(1))
        assert rec.text.strip() == "import asyncio"

    def test_without_color_there_is_no_repaint_to_get_wrong(self):
        console, rec = make_console(color=False)
        busy_terminal.type_out(
            console, "return None", prefix="  12 │ ", language="python", rng=random.Random(1)
        )
        assert "\r" not in rec.text


# ── Scenes ───────────────────────────────────────────────────────────────────


class TestScenes:
    @pytest.mark.parametrize("name", busy_terminal.SCENES)
    def test_every_scene_paints_something_and_asks_to_wait(self, name):
        console, rec = make_console()
        busy_terminal.SCENE_RUNNERS[name](console, random.Random(11))
        assert rec.text.strip()
        assert rec.slept > 0

    @pytest.mark.parametrize("name", busy_terminal.SCENES)
    def test_no_scene_emits_ansi_when_color_is_off(self, name):
        console, rec = make_console(color=False)
        busy_terminal.SCENE_RUNNERS[name](console, random.Random(12))
        assert not ANSI.search(rec.text)

    @pytest.mark.parametrize("name", busy_terminal.SCENES)
    def test_speed_scales_the_wait_down(self, name):
        _, slow = make_console(speed=1.0)
        _, fast = make_console(speed=10.0)
        slow_console = busy_terminal.Console(color=False, speed=1.0, write=slow.write, sleep=slow.sleep)
        fast_console = busy_terminal.Console(color=False, speed=10.0, write=fast.write, sleep=fast.sleep)

        busy_terminal.SCENE_RUNNERS[name](slow_console, random.Random(13))
        busy_terminal.SCENE_RUNNERS[name](fast_console, random.Random(13))

        assert fast.slept == pytest.approx(slow.slept / 10.0, rel=1e-6)

    @pytest.mark.parametrize("name", busy_terminal.SCENES)
    def test_same_seed_replays_the_same_transcript(self, name):
        console_a, rec_a = make_console(color=True)
        console_b, rec_b = make_console(color=True)
        busy_terminal.SCENE_RUNNERS[name](console_a, random.Random(99))
        busy_terminal.SCENE_RUNNERS[name](console_b, random.Random(99))
        assert rec_a.text == rec_b.text

    def test_scenes_touch_no_process_socket_or_file(self):
        """The premise of the skill: none of this output is real."""
        console, _ = make_console()
        with (
            mock.patch("subprocess.run") as run,
            mock.patch("subprocess.Popen") as popen,
            mock.patch("socket.socket") as sock,
            mock.patch("builtins.open") as opened,
        ):
            for runner in busy_terminal.SCENE_RUNNERS.values():
                runner(console, random.Random(14))

        run.assert_not_called()
        popen.assert_not_called()
        sock.assert_not_called()
        opened.assert_not_called()


# ── Run loop ─────────────────────────────────────────────────────────────────


class TestRunLoop:
    def test_stops_once_the_duration_has_elapsed(self):
        console, _ = make_console()
        played = busy_terminal.run(
            console, random.Random(5), duration=3.0, now=ticking_clock(1.0)
        )
        assert played == 3

    def test_a_pinned_scene_is_the_only_one_that_plays(self):
        console, _ = make_console()
        seen: list[str] = []
        runners = {name: (lambda c, r, n=name: seen.append(n)) for name in busy_terminal.SCENES}

        with mock.patch.object(busy_terminal, "SCENE_RUNNERS", runners):
            busy_terminal.run(
                console, random.Random(6), scene="tests", duration=4.0, now=ticking_clock(1.0)
            )

        assert set(seen) == {"tests"}

    def test_cycling_covers_every_scene_without_adjacent_repeats(self):
        console, _ = make_console()
        seen: list[str] = []
        runners = {name: (lambda c, r, n=name: seen.append(n)) for name in busy_terminal.SCENES}

        with mock.patch.object(busy_terminal, "SCENE_RUNNERS", runners):
            busy_terminal.run(console, random.Random(7), duration=40.0, now=ticking_clock(1.0))

        assert set(seen) == set(busy_terminal.SCENES)
        assert all(a != b for a, b in zip(seen, seen[1:]))


# ── CLI ──────────────────────────────────────────────────────────────────────


class TestColorDetection:
    def test_a_tty_that_was_asked_for_color_gets_it(self):
        stream = mock.Mock(isatty=mock.Mock(return_value=True))
        assert busy_terminal.supports_color(stream, requested=True) is True

    def test_a_pipe_never_gets_color(self):
        stream = mock.Mock(isatty=mock.Mock(return_value=False))
        assert busy_terminal.supports_color(stream, requested=True) is False

    def test_no_color_env_wins_over_a_tty(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        stream = mock.Mock(isatty=mock.Mock(return_value=True))
        assert busy_terminal.supports_color(stream, requested=True) is False

    def test_opting_out_wins_over_a_tty(self):
        stream = mock.Mock(isatty=mock.Mock(return_value=True))
        assert busy_terminal.supports_color(stream, requested=False) is False


class TestCli:
    def test_defaults_run_forever_and_cycle(self):
        args = busy_terminal.build_parser().parse_args([])
        assert args.duration == 0.0
        assert args.scene == ""

    def test_scene_is_restricted_to_known_scenes(self):
        with pytest.raises(SystemExit):
            busy_terminal.build_parser().parse_args(["--scene", "nope"])

    def test_a_bounded_run_exits_zero_and_prints(self, capsys):
        code = busy_terminal.main(
            ["--scene", "build", "--duration", "0.001", "--speed", "5000", "--no-color"]
        )
        out = capsys.readouterr().out
        assert code == 0
        assert out.strip()
        assert not ANSI.search(out)

    def test_interrupting_restores_the_cursor_and_exits_cleanly(self):
        with mock.patch.object(busy_terminal, "run", side_effect=KeyboardInterrupt):
            assert busy_terminal.main(["--no-color"]) == 0
