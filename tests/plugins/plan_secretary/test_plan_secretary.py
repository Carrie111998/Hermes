"""plan-secretary tests: precise capture filter + promise_gate + session switch.

Run with:  python -m pytest tests/plugins/plan_secretary/ -q
"""
from __future__ import annotations

import importlib.util
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
_tmp = Path(tempfile.mkdtemp(prefix="ps_pytest_"))
ps._state_dir = lambda: _tmp


POSITIVE = [
    "小墨接下来会检查 logs/plan_secretary_test.jsonl，并把误抓规则写进过滤器。",
    "我接下来会检查 logs/real_task.jsonl，并把结果写进汇报。",
    "接下来我会继续\n回灌 P4 seed 到学习表。\n写完整 P4 阶段复盘。",
    "下一步应是我验收 P5 map，再决定是否让 Codex 生成 P5 materializer。",
    "下一步不是继续烧 P4，而是回灌 P4 seed，形成 P5 新方向。",
]

NEGATIVE = [
    "/new 名称 可以让新会话后续更好恢复。",
    "下一步一般可以考虑把会话拆短。",
    "这个设计后续会更清晰。",
    "小墨说：“我接下来会检查 logs/a.jsonl”",
    "2. alpha 工厂那种\"总结里带下一步\"的句子（如\"下一步应是我验收 P5 map…\"）",
    "真实承诺（下一步应是我验收 P5 map…）→ 正常抓 ✅",
    "api pending(): {'count': 1, 'recent': [...]}",
    "下一步我要检查下未完成的任务。\n取消该承诺。原因：这是测试句",
    "【小秘书】[内部-小秘书承诺确认｜必须立即处理]\n你承诺了：\n  - 我会写 P4 Codex mission",
    "小秘书关",
]


def test_capture_accepts_commitments():
    for s in POSITIVE:
        assert ps._extract_commitments(s), s


def test_capture_rejects_noise():
    for s in NEGATIVE:
        assert not ps._extract_commitments(s), s


def test_promise_gate_triggers_and_dedupes():
    r = ps._on_promise_gate(session_id="P", final_response="下一步应是我验收 P5 map。", attempt=0)
    assert r.get("action") == "continue"
    assert r["message"].startswith(ps.PS_MARKER)
    r2 = ps._on_promise_gate(session_id="P", final_response="下一步应是我验收 P5 map。", attempt=1)
    assert r2.get("action") is None  # dedupe, no loop
    r3 = ps._on_promise_gate(session_id="P", final_response="下一步我会把 P5 复盘写入知识库。", attempt=1)
    assert r3.get("action") == "continue"  # new promise in attempt>0


def test_session_switch():
    assert ps._session_enabled("Q") is True
    assert ps._maybe_handle_switch_command("Q", "小秘书关") is True
    assert ps._session_enabled("Q") is False
    assert ps._on_promise_gate(session_id="Q", final_response="我接下来会检查 logs/a.jsonl", attempt=0) == {}
    assert ps._on_pre_llm_call(session_id="Q", user_message="hi") == {}
    assert ps._maybe_handle_switch_command("Q", "小秘书开") is True
    assert ps._session_enabled("Q") is True


def test_self_capture_prevented():
    r = ps._on_promise_gate(session_id="R", final_response="下一步我会写 P4 mission。", attempt=0)
    assert ps._extract_commitments(r["message"]) == []
