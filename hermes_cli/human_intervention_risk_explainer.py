"""Advisory risk explainer for high-risk CLI human-intervention prompts.

When a high/critical-risk command needs approval, this module produces a short
Chinese explanation of WHY it is dangerous. It uses an LLM when one is provided
and a deterministic static fallback otherwise.

This explanation is ADVISORY ONLY. It must never affect risk classification or
approval semantics, and it must never raise: every failure path degrades to a
static string (or empty string), so callers can use the result unconditionally.
"""

from __future__ import annotations

from typing import Callable

from hermes_cli.human_intervention_notifications import _redact_and_truncate

# Risk levels that justify spending an LLM call on an explanation.
_LLM_RISK_LEVELS = frozenset({"high", "critical"})

# Map known pattern keys to concise Chinese danger phrases. Keys mirror the
# pattern identifiers produced by the command risk classifier.
_PATTERN_DANGER_PHRASES: dict[str, str] = {
    "destructive_delete": "递归删除文件/目录，可能不可逆",
    "rm_rf": "递归删除文件/目录，可能不可逆",
    "git_reset": "会丢弃未提交或未跟踪的改动",
    "git_clean": "会丢弃未提交或未跟踪的改动",
    "chmod": "修改权限/属主，可能影响访问控制",
    "chown": "修改权限/属主，可能影响访问控制",
    "network_pipe_shell": "从网络下载内容直接执行，存在供应链风险",
    "credential_edit": "修改凭据/密钥文件，可能影响认证",
    "force_push": "强制推送会覆盖远端历史",
    "db_drop": "删除数据库/表，数据不可恢复",
}

_GENERIC_HIGH_RISK = "此命令风险较高，可能造成不可逆或影响系统的后果，请谨慎确认。"


def _cap(text: str, max_chars: int) -> str:
    """Cap text to max_chars characters, using an ellipsis when truncated."""
    value = str(text or "").strip()
    if max_chars <= 0:
        return ""
    if len(value) <= max_chars:
        return value
    return value[: max(0, max_chars - 1)].rstrip() + "…"


def _static_explanation(
    *,
    risk_level: str,
    description: str,
    pattern_keys: list[str] | None,
) -> str:
    """Build a deterministic Chinese explanation from keys + description."""
    phrases: list[str] = []
    seen: set[str] = set()
    for key in pattern_keys or []:
        phrase = _PATTERN_DANGER_PHRASES.get(str(key).strip())
        if phrase and phrase not in seen:
            seen.add(phrase)
            phrases.append(phrase)

    desc = str(description or "").strip()
    normalized_level = str(risk_level or "").strip().lower()
    is_high = normalized_level in _LLM_RISK_LEVELS

    if phrases:
        body = "；".join(phrases)
        if desc:
            return f"{body}（{desc}）。"
        return f"{body}。"

    if desc and is_high:
        return f"此命令风险较高：{desc}。"

    if is_high:
        return _GENERIC_HIGH_RISK

    if desc:
        return desc

    return ""


def _build_prompt(*, redacted_command: str, description: str, risk_level: str, max_chars: int) -> str:
    """Build a concise Chinese instruction prompt for the LLM."""
    desc = str(description or "").strip()
    level = str(risk_level or "").strip()
    lines = [
        "你是命令风险审查助手。下面是一条需要用户批准的高风险命令。",
        f"风险级别：{level}" if level else "风险级别：高",
    ]
    if desc:
        lines.append(f"命令说明：{desc}")
    lines.append(f"命令：{redacted_command}")
    lines.append(
        "请用简洁中文，只说明这条命令为什么危险、可能造成什么不可逆后果。"
        "不要建议是否批准，不要输出任何思考过程，"
        f"不超过 {max_chars} 字。"
    )
    return "\n".join(lines)


def explain_command_risk(
    *,
    command: str,
    description: str = "",
    risk_level: str = "",
    pattern_keys: list[str] | None = None,
    llm_fn: Callable[[str, int], str] | None = None,
    max_chars: int = 280,
    timeout_seconds: int = 3,
) -> str:
    """Return a short Chinese explanation of why a command is risky.

    Advisory only — never raises and never affects approval semantics. For
    low/medium/empty risk levels the LLM is never invoked.
    """
    try:
        normalized_level = str(risk_level or "").strip().lower()
        static = _static_explanation(
            risk_level=risk_level,
            description=description,
            pattern_keys=pattern_keys,
        )

        # Only spend an LLM call for high/critical risk.
        if llm_fn is not None and normalized_level in _LLM_RISK_LEVELS:
            redacted_command = _redact_and_truncate(command, max_chars=max_chars)
            prompt = _build_prompt(
                redacted_command=redacted_command,
                description=description,
                risk_level=risk_level,
                max_chars=max_chars,
            )
            try:
                llm_result = llm_fn(prompt, timeout_seconds)
            except Exception:
                llm_result = ""
            if isinstance(llm_result, str) and llm_result.strip():
                return _cap(llm_result, max_chars)

        return _cap(static, max_chars)
    except Exception:
        # Defensive: explainer must never raise.
        return ""
