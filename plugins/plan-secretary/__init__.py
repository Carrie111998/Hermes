"""plan-secretary hook plugin — capture promises, remind the agent to act.

Implements the "promise → confirm → remind → resolve" loop inside Hermes
using only official plugin hooks (zero core patches):

- ``post_llm_call`` — after the agent produces its final reply, scan it for
  future commitments (actor + action verb + concrete object) and store them
  as *pending* captures. The agent's own words are the trigger.
- ``pre_llm_call`` — before the next LLM turn, inject a compact "internal
  reminder" into the user message listing pending captures awaiting
  confirmation and due plans awaiting execution. The agent (小墨) then
  responds via the task-management CLI (confirm/complete/defer/cancel).

Reminders are injected as user-message context (not system prompt), so the
prompt cache prefix is preserved. All state lives under
``get_hermes_home()/state/plan_secretary/``.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

_STATE_DIR_NAME = "plan_secretary"
_PENDING_FILE = "pending_captures.json"
_REGISTRY_FILE = "plan_registry.json"
_NOTIFICATION_FILE = "notification_state.json"

# Marker the agent can use to acknowledge/turn off reminders in one turn.
ACK_MARKER = "[PS-ACK]"

# Uniform marker prefixed to ALL Plan Secretary injected text (promise-gate
# prompts, pre_llm_call reminders). The capture filter skips any text that
# carries this marker, so the secretary never re-captures its own words.
PS_MARKER = "【小秘书】"


# ---------------------------------------------------------------------------
# state helpers (mirror of plan_secretary core, kept dependency-free so the
# plugin loads even if the workspace scripts are absent)
# ---------------------------------------------------------------------------

def _state_dir() -> Path:
    return get_hermes_home() / "state" / _STATE_DIR_NAME


def _read_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _load_captures() -> dict:
    data = _read_json(_state_dir() / _PENDING_FILE, {})
    if not isinstance(data, dict):
        data = {}
    caps = data.get("captures")
    return {
        "version": data.get("version", "plan-secretary-hook-v1"),
        "captures": caps if isinstance(caps, list) else [],
    }


def _load_registry() -> dict:
    data = _read_json(_state_dir() / _REGISTRY_FILE, {})
    if not isinstance(data, dict):
        data = {}
    plans = data.get("plans")
    return {"plans": plans if isinstance(plans, list) else []}


def _session_dir(session_id: str) -> Path:
    return _state_dir() / "sessions" / (session_id or "default")


def _ensure_session(session_id: str) -> None:
    _session_dir(session_id).mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# per-session enabled switch (default ON; user can turn off via "小秘书关")
# ---------------------------------------------------------------------------

_ENABLED_FILE = "enabled.json"


def _session_enabled_path(session_id: str) -> Path:
    return _session_dir(session_id) / _ENABLED_FILE


def _read_session_switch(session_id: str) -> dict:
    data = _read_json(_session_enabled_path(session_id), {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("enabled", True)
    data.setdefault("asked_user", False)
    return data


def _write_session_switch(session_id: str, **changes) -> dict:
    data = _read_session_switch(session_id)
    data.update(changes)
    data["updated_at"] = _now_iso()
    _write_json(_session_enabled_path(session_id), data)
    return data


def _session_enabled(session_id: str) -> bool:
    """Effective switch for a session (default True = ON)."""
    if not session_id:
        return True
    try:
        return bool(_read_session_switch(session_id).get("enabled", True))
    except Exception:
        return True


def _now_iso() -> str:
    from datetime import datetime
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# precise capture (keyword-gated, cheap; no LLM call per message)
# ---------------------------------------------------------------------------

_ACTOR_RE = re.compile(
    r"(?:小墨|我|助手)(?:接下来|稍后|等会|后续|会|将|准备|计划|需要|可以)?|"
    r"\bI\s+(?:will|'ll|am going to|need to|plan to)\b|"
    r"\b(?:assistant|agent)\s+(?:will|can|is going to)\b",
    re.IGNORECASE,
)

_ACTION_WORDS = (
    "检查", "启动", "修复", "生成", "回收", "验证", "跑", "写入", "打开",
    "读取", "扫描", "清理", "更新", "补", "改", "实现", "定位", "测试", "验收",
    "做", "形成", "派", "拦住", "交给", "登记", "执行", "回灌", "写", "设计",
    "复盘", "整理", "填", "灌",
    "register", "schedule", "start", "run", "check", "verify", "fix",
    "write", "update", "scan",
)

_OBJECT_RE = re.compile(
    r"[\w./\\-]+\.(?:py|json|jsonl|log|md|txt|db|yaml|yml)\b|"
    r"[\w./\\-]+\.(?:sh|bat|ps1)\b|"
    r"(?:script|file|process|watcher|log|pending capture|plan|registry|cursor|task|job|pipeline|queue|report|status|review|tracker|codex|seed|direction|settings|delta|deviation|hypothesis)\b|"
    r"(?:脚本|文件|进程|日志|计划|状态文件|过滤器|规则|监听|链路|数据库|会话|消息|小秘书|捕捉|误抓|短测|任务|流水线|队列|报告|结果|状态|执行情况|复盘|决策|知识库|候选|额度|seed|种子|方向|新方向|回灌|学习表|假设|新假设|settings|delta|deviation|model25|model26|\bP[0-9]\b)",
    re.IGNORECASE,
)

_NOISE_RE = re.compile(
    r"^\s*[/`].*(?:可以|后续|下一步)|"
    r"(?:可以|可用于|用于|建议|一般可以|应该|最好|需要用户|让新会话|会更|更好恢复|更清晰|说明|文档)|"
    r"(?:这是|这类|这个设计|设计说明|设计文档|目标是|用于|管|显示|显示：|支持参数|测试命令|日志内容|短测暴露|当前结论|推荐测试句|反例|正例[，：:].*)|"
    r"(?:不是(?:什么|真的|真实|承诺|任务|计划|这个))|"
    r"(?:下一步一般可以|可以考虑|应该进入|后续好恢复|后续会更清晰)",
    re.IGNORECASE,
)

_INTENT_WORDS = ("接下来", "下一步", "我会", "稍后", "等会", "后续", "我准备", "我计划", "明天", "下次", "我打算", "我需要", "i will", "i'll", "i plan to", "i need to")


def _split_sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?；;])\s*|\r?\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _maybe_handle_switch_command(session_id: str, text: str) -> bool:
    """Handle natural-language switch commands ("小秘书开/关/状态").

    Returns True if the text was a switch command (already handled, do not
    capture as a promise). The command itself is never persisted as pending.
    """
    if not session_id:
        return False
    stripped = (text or "").strip()
    if not re.search(r"小秘书", stripped):
        return False
    if re.search(r"小秘书\s*(?:关闭|关掉|停用|不用|不需要|别|停|关)\s*$", stripped) or re.search(r"关(?:闭|掉)?\s*小秘书", stripped):
        _write_session_switch(session_id, enabled=False)
        logger.info("plan-secretary: session %s switch -> OFF", session_id)
        return True
    if re.search(r"小秘书\s*(?:开启|打开|启用|要|需要|用|开)\s*$", stripped) or re.search(r"开(?:启)?\s*小秘书", stripped):
        _write_session_switch(session_id, enabled=True, asked_user=True)
        logger.info("plan-secretary: session %s switch -> ON", session_id)
        return True
    if re.search(r"小秘书\s*(?:状态|开没开|开了吗|关了吗|是否开启)", stripped):
        return True  # status query handled implicitly by reminder/capture flow
    return False


def _extract_commitments(text: str) -> list[str]:
    """Return sentences that look like the agent committing to future work."""
    # Never capture Plan Secretary's own internal prompts / reflected task ids.
    # Otherwise the secretary starts chasing its own reminders as new promises.
    if "[内部-任务确认]" in text or "[内部-任务唤醒]" in text or "[内部-小秘书承诺确认" in text:
        return []
    # Uniform marker: any text carrying the Plan Secretary marker is the
    # secretary's own injected wording — never a fresh promise by the agent.
    if PS_MARKER in text:
        return []
    # If the agent immediately cancels / labels it as a test in the same reply,
    # it is not a real commitment — do not capture.
    if re.search(r"取消该承诺|不是真实(?:授权|任务)|测试句|用于测试", text):
        return []
    # Never capture data-structure echoes: tool/dict output like
    # "api pending(): {'count': 1, ...}" or "text: '...'" is a report of
    # what was captured, not a fresh promise by the agent.
    if re.search(r"\(\)\{.*['\"](?:count|text|recent|id)['\"]\s*:", text) or re.search(r"\btext:\s*['\"].*['\"]", text) or re.search(r"\{\s*['\"](?:count|status|recent)['\"]\s*:", text):
        return []
    # Test-report echoes: "真实承诺（…）→ 正常抓 ✅" / "expect=..." describe
    # filter verification output, not an agent commitment.
    if re.search(r"→\s*(?:正常抓|不抓|OK|FAIL)|expect=\s*(?:True|False)|(?:✅|❌)\s*$", text) or ("RESULT" in text and ("PASS" in text or "FAIL" in text)):
        return []
    found: list[str] = []
    whole_has_actor = bool(_ACTOR_RE.search(text))
    plan_context = bool(re.search(r"(?:我接下来(?:会继续|会)?(?:的计划)?|接下来我会(?:继续|会)?|接下来的计划|后续我会|我会.*(?:计划|安排))", text))
    # If this text is mostly quoting previous captures / task lists (report
    # echo, reminder text, dedupe output), don't re-capture every item as a
    # fresh promise. We keep real first-person commitments only.
    quoted_or_echo = bool(re.search(r"[“\"']|→|->|提取|dedupe|正常捕获|pending now|总 \d+ 条", text))
    leading_echo = bool(re.search(r"^\s*(?:📌|⏰|\[内部|——|pending|capture-|id |\| |\d+[.)、]\s)", text, re.MULTILINE))
    for sentence in _split_sentences(text):
        stripped = sentence.strip()
        if not stripped or stripped.startswith((
            "capture-", "- capture-", "- ", "extract ", "extract:",
            "extract normal", "extract internal", "TEXT ", "->", "'", '"',
        )):
            continue
        low = sentence.lower()
        if "capture-" in low or "plan-" in low:
            continue
        if "promise_gate_test" in low:
            continue
        if re.match(r"^\d{8}_\d{6}_[0-9a-f]+\s*\|", stripped):
            continue
        if "->" in stripped or "正常捕获" in stripped:
            continue
        if "extract normal" in low or "extract internal" in low or "all_plugin_tests_pass" in low:
            continue
        # Skip example/explanation phrasing ("如/比如/例如/像/那种…句子"),
        # which describes a hypothetical rather than committing to work.
        if re.search(r"^\s*(?:如|比如|例如|像|类似|比方说)|那种.*(?:句子|情况|场景)|(?:句子|情况|场景)[（(]如", sentence):
            continue
        if re.search(r"(?:如果|可以|建议|一般|应该|最好|需要用户|可以考虑)", sentence) and not re.search(r"(?:小墨|我|助手)(?:接下来|稍后|等会|后续|会|将|准备|计划|需要|要)", sentence):
            continue
        has_intent = any(kw in low for kw in _INTENT_WORDS)
        has_action = any(w in low for w in _ACTION_WORDS)
        has_object = bool(_OBJECT_RE.search(sentence))
        # Skip reported speech ("小墨说：'我会…'"), which quotes a commitment
        # rather than making one.
        if re.search(r"(?:小墨|我|他|她|它|用户|永生)?\s*(?:说|说了|称|表示|写到|回复|写成)[:：]?\s*(?:[“‘\"]|我(?:会|接下来|下一步|要|将))", sentence):
            continue
        # Skip echo of a previously captured promise (quoted/reference text).
        if quoted_or_echo and not has_intent:
            continue
        # In replies like "我接下来的计划: 1. 做A 2. 形成B" or
        # "接下来我会继续\n回灌 P4 seed 到学习表。", the heading carries the
        # future intent while the following lines carry the actions. Under
        # plan_context, any line with action + object is a plan member, even
        # without a numbered prefix or its own intent word.
        in_plan_block = plan_context and (
            re.match(r"^\s*(?:\d+[.)、]|[-*])\s*", stripped)
            or (has_action and has_object)
        )
        if not has_intent and not in_plan_block:
            continue
        if _NOISE_RE.search(sentence):
            continue
        # Actor may appear once at the top of the reply ("我接下来会 1.yyy
        # 2.zzz") while list items drop the subject; accept whole-text actor.
        # Also accept subject-less "下一步不是A而是B" style direction turns:
        # when the sentence opens with a future-intent word and has action +
        # object, the agent is describing its own next move.
        _subjectless_direction = (
            re.match(r"^\s*(?:下一步|接下来|后续|等会|稍后|明天|下次)", stripped)
            and has_action
            and has_object
        )
        if not _ACTOR_RE.search(sentence) and not whole_has_actor and not _subjectless_direction:
            continue
        if not has_action:
            continue
        if not has_object:
            continue
        found.append(sentence.strip())
    return found


# ---------------------------------------------------------------------------
# capture persistence
# ---------------------------------------------------------------------------

def _capture_id(text: str, existing_ids: set[str]) -> str:
    import time as _time
    stamp = _time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^A-Za-z0-9\u4e00-\u9fff]+", "-", text).strip("-").lower()[:24] or "item"
    base = f"capture-{stamp}-{slug}"
    if base not in existing_ids:
        existing_ids.add(base)
        return base
    idx = 2
    while f"{base}-{idx}" in existing_ids:
        idx += 1
    uid = f"{base}-{idx}"
    existing_ids.add(uid)
    return uid


def _persist_pending(session_id: str, sentences: list[str]) -> list[dict]:
    if not sentences:
        return []
    data = _load_captures()
    def _norm_text(t: str) -> str:
        t = re.sub(r"^\s*(?:小墨|助手|agent)[:：]\s*", "", str(t).strip(), flags=re.IGNORECASE)
        t = re.sub(r"\s+", "", t)
        return t.strip("。！？!?；;，,")

    existing_keys = {(_norm_text(str(c.get("text"))), str(c.get("source_session_id") or "")) for c in data["captures"]}
    existing_ids = {str(c.get("id")) for c in data["captures"] if c.get("id")}
    from datetime import datetime
    now = datetime.now().astimezone().replace(microsecond=0).isoformat()
    added: list[dict] = []
    for sentence in sentences:
        key = (_norm_text(sentence), session_id)
        if key in existing_keys:
            continue
        capture = {
            "id": _capture_id(sentence, existing_ids),
            "text": sentence,
            "source_session_id": session_id,
            "source": "plugin-hook",
            "source_id": f"hook:{session_id}:{now}",
            "source_role": "assistant",
            "created_at": now,
            "updated_at": now,
            "status": "pending",
        }
        data["captures"].append(capture)
        existing_keys.add(key)
        added.append(capture)
    if added:
        _write_json(_state_dir() / _PENDING_FILE, data)
        _ensure_session(session_id)
        _write_json(_session_dir(session_id) / "last_capture.json",
                    {"captured_at": now, "count": len(added)})
    return added


# ---------------------------------------------------------------------------
# reminder assembly (injected into next user message via pre_llm_call)
# ---------------------------------------------------------------------------

def _notify_state(session_id: str) -> dict:
    path = _session_dir(session_id) / _NOTIFICATION_FILE
    data = _read_json(path, {})
    if not isinstance(data, dict):
        data = {}
    data.setdefault("notified_captures", {})
    data.setdefault("due_reminders", {})
    return data


def _build_reminder(session_id: str) -> str:
    """Return internal reminder text for this session, or '' if nothing."""
    from datetime import datetime, timedelta
    now = datetime.now().astimezone()
    parts: list[str] = []

    # 1. pending captures awaiting confirmation (this session only)
    data = _load_captures()
    session_pending_raw = [
        c for c in data["captures"]
        if c.get("status") == "pending" and (c.get("source_session_id") or "") == session_id
    ]
    seen_pending: set[str] = set()
    session_pending = []
    for c in session_pending_raw:
        norm = re.sub(r"^\s*(?:小墨|助手|agent)[:：]\s*", "", str(c.get("text") or "").strip(), flags=re.IGNORECASE)
        norm = re.sub(r"\s+", "", norm).strip("。！？!?；;，,")
        if norm in seen_pending:
            continue
        seen_pending.add(norm)
        session_pending.append(c)
    if session_pending:
        lines = [f"{PS_MARKER}📌 [内部-任务确认] 小墨，你之前承诺了以下后续任务，请确认："]
        for c in session_pending[:5]:
            lines.append(f"  - {c.get('text')}")
        lines.append("  请选择：立即执行 / 登记为定时任务（说明时间节点）/ 取消。")
        parts.append("\n".join(lines))

    # 2. due plans awaiting execution (this session only)
    registry = _load_registry()
    session_active = [
        p for p in registry["plans"]
        if p.get("status") in ("active", "deferred", "blocked")
        and (p.get("source_session_id") or "") == session_id
    ]
    due_plans = []
    for plan in session_active:
        due = plan.get("due") or ""
        try:
            due_dt = datetime.fromisoformat(due.replace("Z", "+00:00")).astimezone()
        except Exception:
            continue
        if due_dt <= now:
            due_plans.append(plan)
    if due_plans:
        lines = ["⏰ [内部-任务唤醒] 小墨，以下任务已到执行时间点："]
        for plan in due_plans[:5]:
            lines.append(f"  - {plan.get('id')} | {plan.get('title')} | 下一步: {plan.get('next_action') or '-'}")
        lines.append("  请执行或说明延后/取消。")
        parts.append("\n".join(lines))

    if not parts:
        return ""
    return "\n\n".join(parts)


def _on_post_llm_call(
    session_id: str = "",
    assistant_response: str = "",
    **kwargs,
) -> None:
    """Capture commitments from the agent's final reply."""
    if not session_id or not assistant_response:
        return
    if not _session_enabled(session_id):
        return
    try:
        # Natural-language switch command: "小秘书开/关/状态" is handled here
        # and never captured as a promise.
        if _maybe_handle_switch_command(session_id, assistant_response):
            return
        # If the agent explicitly cancels a test/quoted commitment, close the
        # matching pending capture instead of re-reminding forever.
        if re.search(r"取消该承诺|不是真实.*任务|测试句|用于测试", assistant_response):
            data = _load_captures()
            changed = False
            for c in data["captures"]:
                if c.get("status") == "pending" and (c.get("source_session_id") or "") == session_id:
                    c["status"] = "ignored"
                    c["ignore_reason"] = "agent cancelled promise/test phrase"
                    changed = True
            if changed:
                _write_json(_state_dir() / _PENDING_FILE, data)
            return
        sentences = _extract_commitments(assistant_response)
        if sentences:
            added = _persist_pending(session_id, sentences)
            if added:
                logger.info("plan-secretary: captured %d promise(s) in session %s", len(added), session_id)
    except Exception as exc:
        logger.warning("plan-secretary post_llm_call failed: %s", exc)


def _on_promise_gate(
    session_id: str = "",
    final_response: str = "",
    attempt: int = 0,
    **kwargs,
) -> dict:
    """Same-turn gate: if the attempted final answer contains promises,
    immediately keep the turn going so the agent confirms/executes them now.

    Unlike pre_llm_call, this fires before the turn is allowed to finish.
    attempt > 0 means we are already in a promise-gate continuation loop; we
    still look for *new* promises (not yet pending) so summary replies that
    introduce a fresh "next step" are not missed, but we never re-prompt for
    the same promise twice (dedupe in _persist_pending handles that).
    """
    if not session_id or not final_response:
        return {}
    if not _session_enabled(session_id):
        return {}
    try:
        if _maybe_handle_switch_command(session_id, final_response):
            return {}
        promises = _extract_commitments(final_response)
        if not promises:
            return {}
        added = _persist_pending(session_id, promises)
        if not added:
            # Nothing new -> allow the turn to finish (no loop).
            return {}
        promise_lines = "\n".join(f"  - {p}" for p in added[:5])
        return {
            "action": "continue",
            "message": (
                f"{PS_MARKER}[内部-小秘书承诺确认｜必须立即处理]\n"
                "你刚刚在对用户的回复中承诺了后续任务：\n"
                f"{promise_lines}\n\n"
                "现在不要结束本轮。请立刻判断并执行以下三选一：\n"
                "1) 如果这些任务应马上做：现在直接执行，直到完成，并在完成后说明已完成；\n"
                "2) 如果需要稍后做：明确给出时间节点/条件，并登记为小秘书任务；\n"
                "3) 如果这只是措辞或已不需要做：取消该承诺并说明原因。\n"
                "不要只说计划；必须执行、登记或取消。"
            ),
        }
    except Exception as exc:
        logger.warning("plan-secretary promise_gate failed: %s", exc)
        return {}


def _on_pre_llm_call(
    session_id: str = "",
    user_message: str = "",
    **kwargs,
) -> dict:
    """Inject pending confirmations / due reminders before the next LLM turn."""
    if not session_id:
        return {}
    if not _session_enabled(session_id):
        return {}
    try:
        # First-run prompt: on the very first LLM turn of a session (no switch
        # state recorded yet), ask the USER once whether to keep the secretary
        # on. Default is ON; if the user ignores the prompt it stays ON.
        switch = _read_session_switch(session_id)
        if not switch.get("asked_user"):
            _write_session_switch(session_id, asked_user=True)
            first_run = (
                f"{PS_MARKER}[小秘书·首次启用] 本会话默认启用小秘书（捕捉并确认小墨的未来承诺）。"
                "如不需要，回复：小秘书关。"
            )
            reminder = _build_reminder(session_id)
            if reminder:
                return {"context": f"{first_run}\n\n{reminder}"}
            return {"context": first_run}
        reminder = _build_reminder(session_id)
        if not reminder:
            return {}
        # Don't re-inject if the user just acked or the same reminder is fresh.
        if ACK_MARKER in (user_message or ""):
            return {}
        logger.debug("plan-secretary: injecting reminder for session %s", session_id)
        return {"context": reminder}
    except Exception as exc:
        logger.warning("plan-secretary pre_llm_call failed: %s", exc)
        return {}


def register(ctx) -> None:
    """Register both hooks with the plugin context."""
    ctx.register_hook("promise_gate", _on_promise_gate)
    ctx.register_hook("post_llm_call", _on_post_llm_call)
    ctx.register_hook("pre_llm_call", _on_pre_llm_call)
    logger.info("plan-secretary hook plugin registered")
