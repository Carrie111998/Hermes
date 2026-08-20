import json
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.task_card import TaskCard
import pipeline.mine as mine


# ── helpers ───────────────────────────────────────────────────────────────


def _session(sid="sess_1", messages=None, **kw):
    base = {"id": sid, "title": kw.pop("title", ""), "cwd": kw.pop("cwd", "/tmp/proj"), "started_at": 123.0}
    base.update(kw)
    base["messages"] = messages or []
    return base


def _msg(role, content="", **kw):
    m = {"role": role, "content": content}
    m.update(kw)
    return m


# ── TaskCard ──────────────────────────────────────────────────────────────


def test_task_card_to_dict_truncates():
    c = TaskCard("s", "id123", "x" * 5000, ["e1", "e2", "e3", "e4", "e5", "e6"], [], 1.0)
    d = c.to_dict()
    assert len(d["user_request"]) == 2000
    assert len(d["friction_evidence"]) == 5
    assert d["tool_calls"] == 0


def test_task_card_repr():
    c = TaskCard("skill-a", "abcdefghij" * 4, "req", ["user_correction: hi"], [], 0)
    assert "skill-a" in repr(c)


# ── resolve_after ─────────────────────────────────────────────────────────


def test_resolve_after_iso_passthrough():
    assert mine.resolve_after("2026-08-13T00:00:00Z") == "2026-08-13T00:00:00Z"
    assert mine.resolve_after("2026-01-02") == "2026-01-02"


def test_resolve_after_relative():
    # 7d returns YYYY-MM-DD
    out = mine.resolve_after("7d")
    assert out  # non-empty
    # 24h returns iso
    out2 = mine.resolve_after("24h")
    assert "T" in out2


def test_resolve_after_invalid_fallback():
    assert mine.resolve_after("bogus") == "7d"


# ── export_sessions (mocked subprocess) ───────────────────────────────────


def test_export_sessions_parses_jsonl():
    payload = '{"id":"a","messages":[]}\n{"id":"b","messages":[]}\n'
    mock = MagicMock(returncode=0, stdout=payload, stderr="")
    with patch("pipeline.mine.subprocess.run", return_value=mock) as sp:
        sessions = mine.export_sessions("7d", timeout=5)
        assert len(sessions) == 2
        assert sessions[0]["id"] == "a"
        # should have called with redact
        assert "--redact" in sp.call_args[0][0]


def test_export_sessions_no_redact_flag():
    mock = MagicMock(returncode=0, stdout="", stderr="")
    with patch("pipeline.mine.subprocess.run", return_value=mock) as sp:
        mine.export_sessions("7d", redact=False)
        assert "--redact" not in sp.call_args[0][0]


def test_export_sessions_timeout_returns_empty():
    with patch("pipeline.mine.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="hermes", timeout=1)):
        assert mine.export_sessions("7d", timeout=1) == []


def test_export_sessions_nonzero_returns_empty(capsys):
    mock = MagicMock(returncode=1, stdout="", stderr="boom")
    with patch("pipeline.mine.subprocess.run", return_value=mock):
        assert mine.export_sessions("7d") == []
    assert "WARN" in capsys.readouterr().err


def test_export_sessions_malformed_line_skipped(capsys):
    payload = '{"id":"ok"}\nnot-json\n{"id":"ok2"}\n'
    mock = MagicMock(returncode=0, stdout=payload, stderr="")
    with patch("pipeline.mine.subprocess.run", return_value=mock):
        sessions = mine.export_sessions("7d")
    assert len(sessions) == 2
    assert "WARN" in capsys.readouterr().err


def test_export_sessions_timeout_param_forwarded():
    mock = MagicMock(returncode=0, stdout="", stderr="")
    with patch("pipeline.mine.subprocess.run", return_value=mock) as sp:
        mine.export_sessions("7d", timeout=42)
        assert sp.call_args[1]["timeout"] == 42


# ── detect_friction ───────────────────────────────────────────────────────


def test_detect_correction_cn():
    sess = _session(messages=[_msg("user", "不对，不是这样的"), _msg("assistant", "ok")])
    cards = mine.detect_friction(sess)
    assert len(cards) == 1
    assert any("user_correction" in e for e in cards[0].friction_evidence)


def test_detect_correction_en():
    sess = _session(messages=[_msg("user", "that's wrong, try again"), _msg("assistant", "ok")])
    cards = mine.detect_friction(sess)
    assert len(cards) == 1


def test_detect_tool_error_exit_code():
    sess = _session(
        messages=[
            _msg("user", "do thing"),
            _msg("tool", json.dumps({"output": "fail", "exit_code": 1}), tool_name="terminal"),
        ]
    )
    cards = mine.detect_friction(sess)
    assert len(cards) == 1
    assert any("tool_error" in e for e in cards[0].friction_evidence)


def test_detect_tool_error_keyword():
    sess = _session(
        messages=[
            _msg("user", "run"),
            _msg("tool", "Traceback: exception foo", tool_name="terminal"),
        ]
    )
    cards = mine.detect_friction(sess)
    assert len(cards) == 1


def test_detect_retry_same_request():
    sess = _session(
        messages=[
            _msg("user", "please fix the deploy script"),
            _msg("assistant", "done"),
            _msg("user", "please fix the deploy script"),
            _msg("assistant", "done"),
            _msg("user", "please fix the deploy script"),
        ]
    )
    cards = mine.detect_friction(sess)
    assert len(cards) == 1
    assert any("retry" in e for e in cards[0].friction_evidence)


def test_no_friction_returns_empty():
    sess = _session(messages=[_msg("user", "hi there"), _msg("assistant", "hello!")])
    assert mine.detect_friction(sess) == []


def test_empty_messages_no_card():
    assert mine.detect_friction(_session(messages=[])) == []


def test_get_skill_name_from_title():
    sess = _session(title="skill: my-skill — task", messages=[_msg("user", "不对")])
    cards = mine.detect_friction(sess)
    assert cards[0].skill_name == "my-skill"


def test_tool_retry_same_tool_multiple_errors():
    sess = _session(
        messages=[
            _msg("user", "deploy"),
            _msg("tool", "error: timeout", tool_name="terminal"),
            _msg("tool", "error: timeout again", tool_name="terminal"),
        ]
    )
    cards = mine.detect_friction(sess)
    assert any("tool_retry" in e for e in cards[0].friction_evidence)


# ── deduplicate ───────────────────────────────────────────────────────────


def test_deduplicate_keeps_best():
    c1 = TaskCard("s", "a", "req", ["user_correction: a"], [], 1)
    c2 = TaskCard("s", "b", "req", ["user_correction: a", "tool_error: x"], [], 2)
    c3 = TaskCard("other", "c", "req", ["user_correction: a"], [], 3)
    out = mine.deduplicate([c1, c2, c3])
    # s::user_correction bucket keeps c2 (more evidence), other keeps c3
    assert len(out) == 2
    # find s bucket
    s_cards = [x for x in out if x.skill_name == "s"]
    assert s_cards[0].session_id == "b"


# ── write_task_cards ──────────────────────────────────────────────────────


def test_write_task_cards(tmp_path):
    cards = [TaskCard("s", "id1", "hello", ["user_correction: hi"], [], 123.0)]
    out = mine.write_task_cards(cards, str(tmp_path), total_sessions_scanned=5)
    data = json.loads(Path(out).read_text(encoding="utf-8"))
    assert data["total_cards"] == 1
    assert data["total_sessions_scanned"] == 5
    assert data["tasks"][0]["skill_name"] == "s"
    assert "generated_at" in data


# ── CLI: cron skip integration ────────────────────────────────────────────


def test_cron_sessions_skipped_in_pipeline(tmp_path):
    # Simulate export returning one cron + one friction session
    cron = _session("cron_abc", messages=[_msg("user", "不对")])
    good = _session("sess_good", messages=[_msg("user", "不对，错了")])
    payload = "\n".join([json.dumps(cron), json.dumps(good)])
    mock = MagicMock(returncode=0, stdout=payload, stderr="")
    with patch("pipeline.mine.subprocess.run", return_value=mock):
        sessions = mine.export_sessions("7d")
    user_sessions = [s for s in sessions if not str(s.get("id", "")).startswith("cron_")]
    assert len(user_sessions) == 1
    assert user_sessions[0]["id"] == "sess_good"
    cards = []
    for s in user_sessions:
        cards.extend(mine.detect_friction(s))
    assert len(cards) == 1
    out = mine.write_task_cards(mine.deduplicate(cards), str(tmp_path), total_sessions_scanned=len(user_sessions))
    assert Path(out).exists()
