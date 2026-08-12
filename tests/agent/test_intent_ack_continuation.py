"""Intent-ack continuation gate + detector behavior.

Covers the config-driven generalization of the codex intent-ack continuation
(issue #27881): the historical ``codex_responses``-only path is byte-stable
under the default ``"auto"`` mode, while an explicit ``true``/model-list opt-in
extends the "you announced an action but called no tool — keep going" nudge to
every api_mode and relaxes the codebase/workspace requirement so general
autonomous workflows ("I'll run a health check on the server") are caught.

These are invariant assertions about how the mode string and the detector
gates relate, not snapshots of the marker lists.
"""

from types import SimpleNamespace
from typing import Union

from agent.agent_runtime_helpers import (
    intent_ack_continuation_enabled,
    intent_ack_continuation_mode,
    looks_like_codex_intermediate_ack,
)


def _agent(
    mode: Union[str, bool, list] = "auto",
    api_mode="chat_completions",
    model="anthropic/claude-sonnet-4",
):
    # _strip_think_blocks is a no-op for these plain-text fixtures.
    return SimpleNamespace(
        _intent_ack_continuation=mode,
        api_mode=api_mode,
        model=model,
        _strip_think_blocks=lambda c: c,
    )


# The reporter's exact repro (#27881): server-ops task, no filesystem reference.
REPRO_USER = (
    "check the current status of the server, grab the latest error logs, "
    "and let me know if there's anything critical"
)
REPRO_ACK = "I will start by running a health check command on the server to see its current status."

# The codex-coding case the detector was originally built for.
CODE_USER = "review the codebase in /app"
CODE_ACK = "Let me inspect the repository files first."


# ── mode resolution ────────────────────────────────────────────────────────




def test_true_is_all_api_modes():
    for am in ("chat_completions", "anthropic", "codex_responses"):
        assert intent_ack_continuation_mode(_agent(True, am)) == "all"
    for s in ("true", "always", "yes", "on", "ON"):
        assert intent_ack_continuation_mode(_agent(s, "chat_completions")) == "all"








def test_missing_attr_defaults_to_auto():
    bare = SimpleNamespace(api_mode="chat_completions", model="x", _strip_think_blocks=lambda c: c)
    assert intent_ack_continuation_mode(bare) == "off"
    bare_codex = SimpleNamespace(api_mode="codex_responses", model="x", _strip_think_blocks=lambda c: c)
    assert intent_ack_continuation_mode(bare_codex) == "codex_only"


def test_enabled_is_mode_not_off():
    assert intent_ack_continuation_enabled(_agent(True, "chat_completions")) is True
    assert intent_ack_continuation_enabled(_agent("auto", "codex_responses")) is True
    assert intent_ack_continuation_enabled(_agent("auto", "chat_completions")) is False
    assert intent_ack_continuation_enabled(_agent(False, "codex_responses")) is False


# ── detector: workspace requirement ─────────────────────────────────────────




def test_multipart_user_message_does_not_crash_on_workspace_path():
    """#9562: vision requests forward ``user_message`` as a multi-part list.

    The OpenAI-compat API server passes the raw ``content`` field straight
    through for vision turns, so ``user_message`` reaches the detector as
    ``[{type:"text",...}, {type:"image_url",...}]``. The ``require_workspace``
    path flattened it with ``(user_message or "").strip()`` — a truthy list
    survived and ``.strip()`` raised ``AttributeError``, killing the turn.
    The text part still has to drive workspace detection.
    """
    a = _agent("auto", "codex_responses")
    multipart = [
        {"type": "text", "text": CODE_USER},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]
    msgs = [{"role": "user", "content": multipart}]
    # No crash, and the text part ("review the codebase in /app") still
    # satisfies the workspace requirement so the ack fires.
    assert looks_like_codex_intermediate_ack(
        a, multipart, CODE_ACK, msgs, require_workspace=True
    )


def test_all_path_drops_workspace_requirement():
    """The #27881 fix: opted-in turns catch non-codebase intent acks."""
    a = _agent(True, "chat_completions")
    msgs = [{"role": "user", "content": REPRO_USER}]
    assert looks_like_codex_intermediate_ack(
        a, REPRO_USER, REPRO_ACK, msgs, require_workspace=False
    )


# ── detector: guardrails that hold regardless of workspace ───────────────────


def test_real_final_answer_does_not_fire():
    a = _agent(True, "chat_completions")
    final = "Done. The server is healthy and there are no critical errors in the logs."
    msgs = [{"role": "user", "content": REPRO_USER}]
    assert not looks_like_codex_intermediate_ack(a, REPRO_USER, final, msgs, require_workspace=False)


def test_conversational_reply_without_action_verb_does_not_fire():
    a = _agent(True, "chat_completions")
    brainstorm = "I'll help you think through the tradeoffs here."
    msgs = [{"role": "user", "content": "help me decide"}]
    assert not looks_like_codex_intermediate_ack(
        a, "help me decide", brainstorm, msgs, require_workspace=False
    )


def test_opted_in_mid_task_ack_still_fires_after_a_tool_ran():
    """primefit stalls mid-task after todo/terminal — prior tools must not abort."""
    a = _agent(True, "chat_completions")
    msgs = [
        {"role": "user", "content": REPRO_USER},
        {"role": "tool", "content": "health check result"},
    ]
    assert looks_like_codex_intermediate_ack(
        a, REPRO_USER, REPRO_ACK, msgs, require_workspace=False
    )


def test_codex_only_path_still_blocks_after_a_tool_ran():
    a = _agent("auto", "codex_responses")
    msgs = [
        {"role": "user", "content": CODE_USER},
        {"role": "tool", "content": "file list"},
    ]
    assert not looks_like_codex_intermediate_ack(
        a, CODE_USER, CODE_ACK, msgs, require_workspace=True
    )


PT_AUDIT_USER = (
    "audite o Hermes: stalls continue, Discord 50006 e o que falta após o fix"
)


def test_portuguese_vou_ack_fires_when_opted_in():
    a = _agent(True, "chat_completions")
    ack = (
        "Vou retomar a auditoria dos erros do Hermes: carrego o skill de "
        "self-audit, busco a sessão anterior e leio o ground truth do repo."
    )
    msgs = [{"role": "user", "content": PT_AUDIT_USER}]
    assert looks_like_codex_intermediate_ack(
        a, PT_AUDIT_USER, ack, msgs, require_workspace=False
    )


def test_portuguese_continuando_mid_task_ack_after_todo_fires():
    a = _agent(True, "chat_completions")
    ack = "Continuando: stalls pós-12:23, gateway e Discord 50006."
    msgs = [
        {"role": "user", "content": PT_AUDIT_USER},
        {"role": "assistant", "content": "updating todos"},
        {"role": "tool", "content": "todos updated", "tool_name": "todo"},
    ]
    assert looks_like_codex_intermediate_ack(
        a, PT_AUDIT_USER, ack, msgs, require_workspace=False
    )


def test_portuguese_refazendo_after_shell_error_fires():
    a = _agent(True, "chat_completions")
    ack = "Syntax do shell quebrou. Refazendo com comandos simples."
    msgs = [
        {"role": "user", "content": PT_AUDIT_USER},
        {"role": "tool", "content": "syntax error"},
    ]
    assert looks_like_codex_intermediate_ack(
        a, PT_AUDIT_USER, ack, msgs, require_workspace=False
    )


def test_portuguese_agora_next_step_ack_after_config_fix_fires():
    a = _agent(True, "chat_completions")
    ack = "Config já aplicado. Agora Discord 50006 + restart — sem anunciar."
    msgs = [
        {"role": "user", "content": PT_AUDIT_USER},
        {"role": "tool", "content": "config updated"},
    ]
    assert looks_like_codex_intermediate_ack(
        a, PT_AUDIT_USER, ack, msgs, require_workspace=False
    )


def test_portuguese_final_answer_does_not_fire():
    a = _agent(True, "chat_completions")
    final = (
        "Pronto. O gateway está estável, o Discord 50006 está contido e não "
        "há stalls pendentes."
    )
    msgs = [{"role": "user", "content": PT_AUDIT_USER}]
    assert not looks_like_codex_intermediate_ack(
        a, PT_AUDIT_USER, final, msgs, require_workspace=False
    )


def test_portuguese_present_tense_commitment_after_tools():
    a = _agent(True, "chat_completions")
    msgs = [{"role": "tool", "content": "x"}]
    assert looks_like_codex_intermediate_ack(
        a, "user", "Fecho a auditoria e corrijo os erros restantes.", msgs, require_workspace=False
    )
    assert not looks_like_codex_intermediate_ack(
        a, "user", "Próximo: uma ideia criativa sem ação.", msgs, require_workspace=False
    )


def test_20260727_android_escrevo_rodo_stall_fires():
    a = _agent(True, "chat_completions")
    msgs = [
        {"role": "user", "content": "continue as tarefas pendentes"},
        {"role": "tool", "content": "ContentRepository written"},
    ]
    assert looks_like_codex_intermediate_ack(
        a,
        "continue",
        "Escrevo teste OfflineCache e rodo gate Gradle JDK17 agora.",
        msgs,
        require_workspace=False,
    )


def test_20260727_diario_oficial_montando_gravando_stall_fires():
    a = _agent(True, "chat_completions")
    msgs = [
        {"role": "user", "content": "analise isso e me de ideias"},
        {"role": "tool", "content": "browser result"},
    ]
    assert looks_like_codex_intermediate_ack(
        a,
        "analise",
        "Montando o plano completo com veredicto de canal, arquitetura e riscos.",
        msgs,
        require_workspace=False,
    )
    assert looks_like_codex_intermediate_ack(
        a,
        "analise",
        "Evidência suficiente. Gravando plano com canais, arquitetura e riscos reais.",
        msgs,
        require_workspace=False,
    )


def test_completed_portuguese_answer_does_not_false_continue_on_salvo():
    a = _agent(True, "chat_completions")
    msgs = [{"role": "tool", "content": "x"}]
    assert not looks_like_codex_intermediate_ack(
        a,
        "u",
        "Pronto. O plano está salvo e os riscos estão documentados.",
        msgs,
        require_workspace=False,
    )








