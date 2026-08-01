from plugins.platforms.telegram.adapter import _resolve_telegram_channel_prompt


def _extra():
    return {
        "channel_prompts": {
            "-1001": "GROUP BASELINE",
            "17": "LEGACY GLOBAL BOOKS",
        },
        "topic_prompts": {
            "-1001:17": "SCOPED BOOKS POLICY",
        },
    }


def test_scoped_topic_prompt_is_additive_with_group_baseline():
    prompt = _resolve_telegram_channel_prompt(
        _extra(), chat_id="-1001", thread_id="17", chat_type="group"
    )
    assert prompt == "GROUP BASELINE\n\nSCOPED BOOKS POLICY"


def test_unknown_future_topic_falls_back_to_group_baseline():
    prompt = _resolve_telegram_channel_prompt(
        _extra(), chat_id="-1001", thread_id="99", chat_type="group"
    )
    assert prompt == "GROUP BASELINE"


def test_same_thread_id_in_another_group_does_not_receive_scoped_or_legacy_prompt():
    prompt = _resolve_telegram_channel_prompt(
        _extra(), chat_id="-2002", thread_id="17", chat_type="group"
    )
    assert prompt is None


def test_dm_topic_with_same_thread_id_does_not_receive_group_topic_prompt():
    prompt = _resolve_telegram_channel_prompt(
        _extra(), chat_id="429731663", thread_id="17", chat_type="dm"
    )
    assert prompt is None


def test_legacy_resolution_remains_when_scoped_surface_is_absent():
    prompt = _resolve_telegram_channel_prompt(
        {"channel_prompts": {"-1001": "BASE", "17": "LEGACY"}},
        chat_id="-1001",
        thread_id="17",
        chat_type="group",
    )
    assert prompt == "LEGACY"


def test_nested_integer_yaml_keys_resolve_after_normalization():
    prompt = _resolve_telegram_channel_prompt(
        {
            "channel_prompts": {-1001: "GROUP BASELINE"},
            "topic_prompts": {-1001: {17: "SCOPED BOOKS POLICY"}},
        },
        chat_id="-1001",
        thread_id="17",
        chat_type="group",
    )
    assert prompt == "SCOPED BOOKS POLICY"
