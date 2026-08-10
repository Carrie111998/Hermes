"""Wake gate for the Jaum inbox sweeper.

The sweeper runs every 10 minutes and, measured over 400 consecutive runs,
moved nothing on 90.2% of them — each of those still cost a full agent
session. This gate answers one deterministic question: is there anything at
all that could be swept?

The gate is deliberately CONSERVATIVE. Sleeping when there was work would
leave files stranded in the inbox; waking when there was none merely costs
what today's behavior already costs. So every ambiguous case wakes.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
import time

import pytest

GATE = (
    pathlib.Path.home() / ".hermes" / "profiles" / "main" / "scripts"
    / "jaum_inbox_sweep_gate.py"
)

DAY = 86400.0
HOUR = 3600.0


def _load():
    if not GATE.is_file():
        pytest.skip(f"gate not present at {GATE}")
    spec = importlib.util.spec_from_file_location("jaum_inbox_sweep_gate", GATE)
    mod = importlib.util.module_from_spec(spec)
    # @dataclass resolves annotations through sys.modules[cls.__module__];
    # a module loaded by path alone is absent from it and dataclass creation
    # dies with "NoneType has no attribute __dict__".
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return mod


@pytest.fixture
def mod():
    return _load()


@pytest.fixture
def boxes(tmp_path):
    main = tmp_path / "mailbox" / "main" / "inbox"
    cv = tmp_path / "mailbox" / "cv-handler" / "inbox"
    main.mkdir(parents=True)
    cv.mkdir(parents=True)
    return main, cv


def _touch(directory, name, age_hours=0.0, body="{}"):
    p = directory / name
    p.write_text(body, encoding="utf-8")
    stamp = time.time() - age_hours * HOUR
    import os
    os.utime(p, (stamp, stamp))
    return p


def _decide(mod, main, cv):
    return mod.decide(main_inbox=main, cv_inbox=cv, now=time.time())


class TestSleepsOnlyWhenCertain:
    def test_empty_inboxes_sleep(self, mod, boxes):
        main, cv = boxes
        assert _decide(mod, main, cv).wake is False

    def test_steady_state_of_keepers_sleeps(self, mod, boxes):
        """The observed real state: matcher results + batch summary + notification."""
        main, cv = boxes
        _touch(main, "20260810T00_SCORE_RESULT_matcher_VIP-1_a.json", 3)
        _touch(main, "20260810T00_SCORE_RESULT_matcher_VIP-2_b.json", 20)
        _touch(main, "20260810T06_SCORE_BATCH_SUMMARY_matcher.json", 12)
        _touch(main, "20260810T18_NOTIFICATION_sentinel_9c.json", 2)

        assert _decide(mod, main, cv).wake is False

    def test_unknown_file_wakes(self, mod, boxes):
        """Anything the gate cannot positively classify as a keeper wakes."""
        main, cv = boxes
        _touch(main, "20260810T00_MYSTERY_PACKET_zz.json", 1)

        assert _decide(mod, main, cv).wake is True


class TestExpiredKeepersWake:
    def test_matcher_result_past_24h_wakes(self, mod, boxes):
        main, cv = boxes
        _touch(main, "20260808T00_SCORE_RESULT_matcher_VIP-9_c.json", 30)
        assert _decide(mod, main, cv).wake is True

    def test_notification_past_12h_wakes(self, mod, boxes):
        main, cv = boxes
        _touch(main, "20260810T00_NOTIFICATION_sentinel_1.json", 13)
        assert _decide(mod, main, cv).wake is True

    def test_error_past_48h_wakes(self, mod, boxes):
        main, cv = boxes
        _touch(main, "20260807T00_ERROR_matcher_1.json", 50)
        assert _decide(mod, main, cv).wake is True

    def test_fresh_error_is_kept(self, mod, boxes):
        main, cv = boxes
        _touch(main, "20260810T00_ERROR_matcher_1.json", 5)
        assert _decide(mod, main, cv).wake is False

    def test_only_the_newest_error_per_source_is_kept(self, mod, boxes):
        """Policy keeps the newest unresolved ERROR per source; older ones sweep."""
        main, cv = boxes
        _touch(main, "20260810T05_ERROR_applier_new.json", 1)
        _touch(main, "20260810T01_ERROR_applier_old.json", 5)

        decision = _decide(mod, main, cv)
        assert decision.wake is True
        assert any("ERROR_applier_old" in r for r in decision.reasons)
        assert not any("ERROR_applier_new" in r for r in decision.reasons)

    def test_newest_error_per_distinct_source_each_kept(self, mod, boxes):
        main, cv = boxes
        _touch(main, "20260810T05_ERROR_applier_a.json", 1)
        _touch(main, "20260810T05_ERROR_matcher_b.json", 2)

        assert _decide(mod, main, cv).wake is False


class TestUrgencyOverride:
    @pytest.mark.parametrize("word", ["interview", "Offer", "hard deadline"])
    def test_urgent_content_is_always_kept(self, mod, boxes, word):
        """Urgent items are keepers regardless of age — they must not force a wake."""
        main, cv = boxes
        _touch(main, "20260801T00_SCORE_RESULT_matcher_old_x.json", 400,
               body=json.dumps({"note": f"scheduled {word} next week"}))
        assert _decide(mod, main, cv).wake is False

    def test_unreadable_file_wakes(self, mod, boxes):
        """Cannot read it, cannot classify it — never assume it is disposable."""
        main, cv = boxes
        p = _touch(main, "20260801T00_SCORE_RESULT_matcher_old_x.json", 400)
        p.write_bytes(b"\xff\xfe\x00binary-garbage")
        import os
        stamp = time.time() - 400 * HOUR
        os.utime(p, (stamp, stamp))
        assert _decide(mod, main, cv).wake is True


class TestCvHandlerLane:
    def test_file_older_than_7d_wakes(self, mod, boxes):
        main, cv = boxes
        _touch(cv, "20260701T00_KB_QUERY_x.json", 8 * 24)
        assert _decide(mod, main, cv).wake is True

    def test_recent_file_does_not_wake(self, mod, boxes):
        main, cv = boxes
        _touch(cv, "20260810T00_KB_QUERY_x.json", 24)
        assert _decide(mod, main, cv).wake is False


class TestMissingDirectories:
    def test_missing_inboxes_sleep_rather_than_crash(self, mod, tmp_path):
        missing_main = tmp_path / "nope" / "inbox"
        missing_cv = tmp_path / "nope2" / "inbox"
        assert mod.decide(main_inbox=missing_main, cv_inbox=missing_cv,
                          now=time.time()).wake is False


class TestStdoutContract:
    def test_sleep_prints_wake_false_as_last_line(self, mod, boxes, capsys, monkeypatch):
        main, cv = boxes
        monkeypatch.setattr(mod, "MAIN_INBOX", main)
        monkeypatch.setattr(mod, "CV_INBOX", cv)

        assert mod.main() == 0
        last = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()][-1]
        assert json.loads(last) == {"wakeAgent": False}

    def test_wake_prints_wake_true_and_a_reason(self, mod, boxes, capsys, monkeypatch):
        main, cv = boxes
        _touch(main, "20260810T00_MYSTERY_zz.json", 1)
        monkeypatch.setattr(mod, "MAIN_INBOX", main)
        monkeypatch.setattr(mod, "CV_INBOX", cv)

        assert mod.main() == 0
        out = capsys.readouterr().out
        lines = [ln for ln in out.splitlines() if ln.strip()]
        assert json.loads(lines[-1]) == {"wakeAgent": True}
        # The agent should be told why it was woken.
        assert "candidate" in out.lower()
