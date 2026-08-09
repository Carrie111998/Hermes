"""Tests for the plan-secretary plugin: capture filter, promise_gate,
per-session switch, and self-capture prevention. Run without pytest:
    python tests/plugins/plan_secretary/smoke_no_pytest.py
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path

PLUGIN = Path(__file__).resolve().parents[3] / "plugins" / "plan-secretary" / "__init__.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_secretary_hook", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_secretary_hook"] = mod
    spec.loader.exec_module(mod)
    return mod


ps = _load()
_tmp = Path(tempfile.mkdtemp(prefix="ps_test_"))
ps._state_dir = lambda: _tmp

FAILS = []


def check(name: str, got, expect):
    ok = got == expect
    print(f"{name}: got={got} expect={expect} {'OK' if ok else 'FAIL'}")
    if not ok:
        FAILS.append(name)


def test_capture_filter():
    print("\n=== capture filter ===")
    cases = [
        ("real promise", "我接下来会检查 logs/real_task.jsonl，并把结果写进汇报。", True),
        ("plan block", "接下来我会继续\n回灌 P4 seed 到学习表。\n写完整 P4 阶段复盘。", True),
        ("summary next", "下一步应是我验收 P5 map，再决定是否让 Codex 生成 P5 materializer。", True),
        ("subjectless", "下一步不是继续烧 P4，而是回灌 P4 seed，形成 P5 新方向。", True),
        ("reported speech", "小墨说：“我接下来会检查 logs/a.jsonl”", False),
        ("example", "2. alpha 工厂那种\"总结里带下一步\"的句子（如\"下一步应是我验收 P5 map…\"）", False),
        ("test report", "真实承诺（下一步应是我验收 P5 map…）→ 正常抓 ✅", False),
        ("dict echo", "api pending(): {'count': 1, 'recent': [...]}", False),
        ("cancel", "下一步我要检查下未完成的任务。\n取消该承诺。原因：这是测试句", False),
        ("internal marker", "【小秘书】[内部-小秘书承诺确认｜必须立即处理]\n你承诺了：\n  - 我会写 P4 Codex mission", False),
    ]
    for name, text, expect in cases:
        check(f"filter[{name}]", bool(ps._extract_commitments(text)), expect)


def test_promise_gate():
    print("\n=== promise_gate ===")
    r = ps._on_promise_gate(session_id="S", final_response="下一步应是我验收 P5 map。", attempt=0)
    check("gate triggers", r.get("action"), "continue")
    check("gate msg marked", r["message"].startswith(ps.PS_MARKER), True)
    # duplicate -> no re-prompt
    r2 = ps._on_promise_gate(session_id="S", final_response="下一步应是我验收 P5 map。", attempt=1)
    check("gate dedupe", r2.get("action"), None)
    # new promise in attempt>0 -> triggers
    r3 = ps._on_promise_gate(session_id="S", final_response="下一步我会把 P5 复盘写入知识库。", attempt=1)
    check("gate new-in-attempt", r3.get("action"), "continue")
    # self-capture prevention: injected message not re-captured
    check("gate self-skip", ps._extract_commitments(r["message"]), [])


def test_session_switch():
    print("\n=== per-session switch ===")
    check("default on", ps._session_enabled("S2"), True)
    check("handle off", ps._maybe_handle_switch_command("S2", "小秘书关"), True)
    check("off persists", ps._session_enabled("S2"), False)
    check("gate off skipped", ps._on_promise_gate(session_id="S2", final_response="我接下来会检查 logs/a.jsonl", attempt=0), {})
    check("pre off skipped", ps._on_pre_llm_call(session_id="S2", user_message="hi"), {})
    check("handle on", ps._maybe_handle_switch_command("S2", "小秘书开"), True)
    check("on persists", ps._session_enabled("S2"), True)
    check("cmd not captured", ps._extract_commitments("小秘书关"), [])
    # first-run prompt only once
    r = ps._on_pre_llm_call(session_id="S3", user_message="你好")
    check("first-run prompt", "首次启用" in (r.get("context", "") if r else ""), True)
    r2 = ps._on_pre_llm_call(session_id="S3", user_message="你好")
    check("first-run once", (r2 or {}).get("context") or "", "")


def test_state_files():
    print("\n=== state files ===")
    pending = json.loads((_tmp / "pending_captures.json").read_text(encoding="utf-8"))
    n_pending = sum(1 for c in pending["captures"] if c.get("status") == "pending")
    check("pending persisted", n_pending >= 2, True)
    sw = ps._read_session_switch("S2")
    check("switch file", sw.get("enabled"), True)


if __name__ == "__main__":
    test_capture_filter()
    test_promise_gate()
    test_session_switch()
    test_state_files()
    print("\n" + ("ALL PASS" if not FAILS else f"FAILED: {FAILS}"))
    sys.exit(1 if FAILS else 0)
