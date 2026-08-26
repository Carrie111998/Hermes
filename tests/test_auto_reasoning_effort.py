from hermes_constants import (
    VALID_REASONING_EFFORTS,
    parse_reasoning_effort,
    resolve_auto_reasoning_config,
)


def test_parse_reasoning_effort_accepts_auto_marker():
    assert parse_reasoning_effort("auto") == {"enabled": True, "effort": "auto"}


def test_auto_policy_is_not_added_to_static_provider_effort_ladder():
    assert "auto" not in VALID_REASONING_EFFORTS


def test_auto_reasoning_uses_low_for_simple_status_question():
    cfg = {"enabled": True, "effort": "auto"}
    messages = [{"role": "user", "content": "what effort is active?"}]

    assert resolve_auto_reasoning_config(cfg, messages) == {"enabled": True, "effort": "low"}


def test_auto_reasoning_uses_medium_for_design_and_config_changes():
    cfg = {"enabled": True, "effort": "auto"}
    messages = [{"role": "user", "content": "implement an automatic effort config change"}]

    assert resolve_auto_reasoning_config(cfg, messages) == {"enabled": True, "effort": "medium"}


def test_auto_reasoning_uses_high_for_debug_security_and_architecture():
    cfg = {"enabled": True, "effort": "auto"}
    messages = [{"role": "user", "content": "find the root cause of this security architecture failure"}]

    assert resolve_auto_reasoning_config(cfg, messages) == {"enabled": True, "effort": "high"}


def test_auto_reasoning_reads_structured_latest_user_content():
    cfg = {"enabled": True, "effort": "auto"}
    messages = [
        {"role": "user", "content": "old simple question"},
        {"role": "assistant", "content": "answer"},
        {"role": "user", "content": [{"type": "text", "text": "debug this failing test"}]},
    ]

    assert resolve_auto_reasoning_config(cfg, messages) == {"enabled": True, "effort": "high"}


def test_auto_reasoning_leaves_explicit_effort_unchanged():
    cfg = {"enabled": True, "effort": "medium"}

    assert resolve_auto_reasoning_config(cfg, []) is cfg
