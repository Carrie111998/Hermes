from gateway.run import _bound_long_lived_history, _long_lived_history_limit


def test_long_lived_history_keeps_short_history_unchanged():
    history = [{"role": "user", "content": "hello"}]
    assert _bound_long_lived_history(history, 20) is history


def test_long_lived_history_starts_tail_at_user_message():
    history = [
        {"role": "user", "content": f"question {index}"}
        if index % 2 == 0
        else {"role": "assistant", "content": f"answer {index}"}
        for index in range(30)
    ]
    bounded = _bound_long_lived_history(history, 9)
    assert len(bounded) <= 9
    assert bounded[0]["role"] == "user"
    assert bounded[-1] == history[-1]


def test_long_lived_history_does_not_start_with_tool_result():
    history = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "older work"},
        {"role": "assistant", "content": "older result"},
        {"role": "user", "content": "prior work"},
        {"role": "assistant", "content": "prior result"},
        {"role": "assistant", "tool_calls": [{"id": "c1"}]},
        {"role": "tool", "tool_call_id": "c1", "content": "result"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new answer"},
    ]
    bounded = _bound_long_lived_history(history, 8)
    assert bounded[0]["role"] == "user"
    assert bounded[-2:] == [
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "new answer"},
    ]


def test_long_lived_history_limit_is_signal_scoped():
    config = {
        "gateway": {
            "long_lived_history": {
                "enabled": True,
                "platforms": ["signal"],
                "max_messages": 96,
            }
        }
    }
    assert _long_lived_history_limit(config, "signal") == 96
    assert _long_lived_history_limit(config, "telegram") is None
