from hermes_cli.agent_budget import DEFAULT_AGENT_MAX_TURNS, resolve_agent_max_turns


def test_nested_agent_max_turns_wins():
    assert (
        resolve_agent_max_turns(
            {"agent": {"max_turns": 200}, "max_turns": 77},
            environ={"HERMES_MAX_ITERATIONS": "66"},
        )
        == 200
    )


def test_legacy_env_and_default_fallbacks():
    assert resolve_agent_max_turns({"max_turns": 77}, environ={}) == 77
    assert resolve_agent_max_turns({}, environ={"HERMES_MAX_ITERATIONS": "66"}) == 66
    assert resolve_agent_max_turns({}, environ={}) == DEFAULT_AGENT_MAX_TURNS


def test_invalid_values_fail_closed_to_next_source():
    assert (
        resolve_agent_max_turns(
            {"agent": {"max_turns": 0}, "max_turns": "invalid"},
            environ={"HERMES_MAX_ITERATIONS": "55"},
        )
        == 55
    )