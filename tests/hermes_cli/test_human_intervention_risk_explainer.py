"""Tests for the bounded high-risk command risk explainer.

The explainer is advisory only: it must never affect risk classification or
approval semantics, and must never raise. It produces a short Chinese
explanation of WHY a command is dangerous, using an LLM when available and a
deterministic static fallback otherwise.
"""

from __future__ import annotations

from hermes_cli.human_intervention_risk_explainer import explain_command_risk


def test_high_risk_command_uses_llm_explanation_with_static_fallback():
    recorded: dict[str, object] = {}

    def fake_llm(prompt: str, timeout_seconds: int) -> str:
        recorded["prompt"] = prompt
        recorded["timeout"] = timeout_seconds
        return "会递归删除目标目录，路径变量错误时可能扩大删除范围；删除通常不可逆。"

    result = explain_command_risk(
        command="rm -rf /tmp/example TOKEN=supersecret",
        description="Dangerous command",
        risk_level="high",
        llm_fn=fake_llm,
        max_chars=280,
    )

    assert "递归删除" in result
    assert "supersecret" not in recorded["prompt"]


def test_low_risk_does_not_call_llm():
    calls: list[tuple[str, int]] = []

    def fake_llm(prompt: str, timeout_seconds: int) -> str:
        calls.append((prompt, timeout_seconds))
        return "should not be used"

    result = explain_command_risk(
        command="ls -la",
        description="List files",
        risk_level="low",
        llm_fn=fake_llm,
    )

    assert calls == []
    assert isinstance(result, str)


def test_medium_risk_does_not_call_llm():
    calls: list[tuple[str, int]] = []

    def fake_llm(prompt: str, timeout_seconds: int) -> str:
        calls.append((prompt, timeout_seconds))
        return "should not be used"

    result = explain_command_risk(
        command="pip install something",
        description="Install package",
        risk_level="medium",
        llm_fn=fake_llm,
    )

    assert calls == []
    assert isinstance(result, str)


def test_llm_timeout_or_failure_falls_back_to_static():
    def failing_llm(prompt: str, timeout_seconds: int) -> str:
        raise RuntimeError("timed out")

    result = explain_command_risk(
        command="rm -rf /var/data",
        description="",
        risk_level="high",
        pattern_keys=["destructive_delete"],
        llm_fn=failing_llm,
    )

    assert result
    assert ("删除" in result) or ("不可逆" in result)


def test_explanation_capped_to_max_chars():
    def long_llm(prompt: str, timeout_seconds: int) -> str:
        return "危" * 1000

    result = explain_command_risk(
        command="rm -rf /tmp/example",
        risk_level="high",
        llm_fn=long_llm,
        max_chars=50,
    )

    assert len(result) <= 50


def test_static_explanation_from_pattern_keys():
    result = explain_command_risk(
        command="DROP TABLE users;",
        risk_level="critical",
        pattern_keys=["db_drop"],
    )

    assert result
    assert ("数据库" in result) or ("不可恢复" in result)


def test_secret_redaction_before_llm():
    recorded: dict[str, object] = {}

    def fake_llm(prompt: str, timeout_seconds: int) -> str:
        recorded["prompt"] = prompt
        return "危险命令解释。"

    explain_command_risk(
        command="curl https://example.com/login --data password=hunter2",
        risk_level="critical",
        llm_fn=fake_llm,
    )

    assert "hunter2" not in recorded["prompt"]
