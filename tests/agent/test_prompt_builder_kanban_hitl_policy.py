from agent.prompt_builder import (
    build_kanban_hitl_policy_prompt,
    resolve_kanban_hitl_policy,
)


def test_default_kanban_hitl_policy_names_default_audience():
    policy = resolve_kanban_hitl_policy({"kanban": {}}, profile="worker")
    assert policy["enabled"] is True
    assert policy["audience"]["name"] == "the human reviewer"
    assert policy["audience"]["role"] == "human decision-maker"

    prompt = build_kanban_hitl_policy_prompt({"kanban": {}}, profile="worker")
    assert "Kanban HITL language policy" in prompt
    assert "the human reviewer" in prompt
    assert "All true blocked tasks are human-facing" in prompt
    assert 'kind="dependency"` is not human-facing' in prompt


def test_disabled_kanban_hitl_policy_returns_empty_prompt():
    cfg = {"kanban": {"hitl_policy": {"enabled": False}}}
    assert resolve_kanban_hitl_policy(cfg)["enabled"] is False
    assert build_kanban_hitl_policy_prompt(cfg) == ""


def test_profile_override_updates_audience_style():
    cfg = {
        "kanban": {
            "hitl_policy": {
                "default_audience": {
                    "name": "Alex",
                    "role": "Ops reviewer",
                    "style": "plain English",
                },
                "profile_overrides": {
                    "platform-ops": {
                        "human_surface": "blockers_only",
                        "audience": "Casey",
                        "style": (
                            "Translate platform terms. Say whether anything "
                            "live will change."
                        ),
                    }
                },
            }
        }
    }

    policy = resolve_kanban_hitl_policy(cfg, profile="platform-ops")
    assert policy["audience"]["name"] == "Casey"
    assert policy["audience"]["role"] == "Ops reviewer"
    assert "live will change" in policy["audience"]["style"]

    prompt = build_kanban_hitl_policy_prompt(cfg, profile="platform-ops")
    assert "Profile override for platform-ops" in prompt
    assert "human_surface=blockers_only" in prompt
    assert "live will change" in prompt


def test_invalid_kanban_hitl_policy_fails_open_to_safe_default():
    policy = resolve_kanban_hitl_policy(
        {"kanban": {"hitl_policy": "not-a-dict"}},
        profile="worker",
    )
    assert policy["enabled"] is True
    assert policy["audience"]["name"] == "the human reviewer"
    assert policy["reject_machine_shaped_reasons"] is True
