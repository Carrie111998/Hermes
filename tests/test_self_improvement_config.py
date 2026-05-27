from agent.self_improvement import (
    normalize_self_improvement_notify_policy,
    resolve_self_improvement_notify_policy,
)


def test_normalize_self_improvement_notify_policy_aliases():
    assert normalize_self_improvement_notify_policy("channel") == "channel"
    assert normalize_self_improvement_notify_policy("chat") == "channel"
    assert normalize_self_improvement_notify_policy(True) == "channel"
    assert normalize_self_improvement_notify_policy("operator-log") == "operator_log"
    assert normalize_self_improvement_notify_policy("logs") == "operator_log"
    assert normalize_self_improvement_notify_policy("off") == "off"
    assert normalize_self_improvement_notify_policy(False) == "off"


def test_resolve_self_improvement_notify_policy_prefers_nested_config():
    config = {
        "agent": {
            "self_improvement_notify": "channel",
            "self_improvement": {"notify": "operator_log"},
        }
    }
    assert resolve_self_improvement_notify_policy(config) == "operator_log"


def test_resolve_self_improvement_notify_policy_supports_flat_compat_key():
    assert resolve_self_improvement_notify_policy(
        {"agent": {"self_improvement_notify": "off"}}
    ) == "off"
