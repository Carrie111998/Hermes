from types import SimpleNamespace

from gateway.platforms.base import resolve_group_topic
from gateway.config import Platform
from gateway.session import SessionContext, SessionSource, build_session_context_prompt
from gateway.skill_policy import SkillPolicyStatus, resolve_enabled_skills_policy
from gateway.toolset_policy import (
    ToolsetPolicyStatus,
    resolve_enabled_toolsets_policy,
)
from gateway.run import GatewayRunner


def _source(chat="-100", thread="20"):
    return SimpleNamespace(
        platform=SimpleNamespace(value="telegram"),
        chat_id=chat,
        thread_id=thread,
    )


def _config(topic):
    return {
        "platforms": {
            "telegram": {
                "extra": {
                    "group_topics": [
                        {"chat_id": "-100", "topics": [topic]}
                    ]
                }
            }
        }
    }


def test_cross_thread_inherits_without_rewriting_physical_source():
    topic = {
        "thread_id": "10",
        "cross_thread": ["20"],
        "enabled_toolsets": ["terminal"],
    }
    source = _source()
    extra = _config(topic)["platforms"]["telegram"]["extra"]
    assert resolve_group_topic(extra, source.chat_id, source.thread_id) is topic
    assert source.thread_id == "20"
    assert resolve_group_topic(extra, "-200", "20") is None


def test_direct_child_topic_wins_over_canonical_cross_thread():
    extra = {
        "group_topics": [
            {
                "chat_id": "-100",
                "topics": [
                    {"thread_id": "10", "cross_thread": ["20"], "name": "parent"},
                    {"thread_id": "20", "name": "child"},
                ],
            }
        ]
    }
    assert resolve_group_topic(extra, "-100", "20")["name"] == "child"


def test_enabled_skills_is_canonical_nonempty_allowlist():
    cfg = _config(
        {"thread_id": "10", "cross_thread": ["20"], "enabled_skills": ["audit"]}
    )
    valid = resolve_enabled_skills_policy(
        _source(), cfg, skill_names={"audit", "coding"}
    )
    assert valid.status is SkillPolicyStatus.CONFIGURED_VALID
    assert valid.identities == ("audit",)

    invalid = resolve_enabled_skills_policy(
        _source(), _config({"thread_id": "20", "enabled_skills": []})
    )
    assert invalid.status is SkillPolicyStatus.CONFIGURED_INVALID


def test_enabled_toolsets_empty_valid_unknown_fails_closed():
    empty = resolve_enabled_toolsets_policy(
        _source(), _config({"thread_id": "20", "enabled_toolsets": []}),
        known_toolsets={"terminal"},
    )
    assert empty.status is ToolsetPolicyStatus.CONFIGURED_VALID
    assert empty.toolsets == ()

    unknown = resolve_enabled_toolsets_policy(
        _source(), _config({"thread_id": "20", "enabled_toolsets": ["missing"]}),
        known_toolsets={"terminal"},
    )
    assert unknown.status is ToolsetPolicyStatus.CONFIGURED_INVALID


def test_runtime_identity_appears_once_in_session_context():
    context = SessionContext(
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="chat-1",
            chat_type="group",
            thread_id="thread-2",
        ),
        connected_platforms=[Platform.TELEGRAM],
        home_channels={},
        session_id="session-3",
        model="gpt-test",
        provider="provider-test",
    )
    prompt = build_session_context_prompt(context, redact_pii=False)
    assert "**Session_ID:** `session-3`" in prompt
    assert "**Thread_ID:** `thread-2`" in prompt
    assert "**Chat_ID:** `\"chat-1\"`" in prompt
    assert "**Platform:** `telegram`" in prompt
    assert prompt.count("**Model:** `gpt-test`") == 1
    assert prompt.count("**Provider:** `provider-test`") == 1


def test_redaction_preserves_telegram_group_chat_id_only():
    group_context = SessionContext(
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="-1003914667905",
            chat_type="group",
            user_id="123456",
        ),
        connected_platforms=[Platform.TELEGRAM],
        home_channels={},
    )
    group_prompt = build_session_context_prompt(group_context, redact_pii=True)

    assert '**Chat_ID:** `"-1003914667905"`' in group_prompt
    assert "123456" not in group_prompt

    dm_context = SessionContext(
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="123456",
            chat_type="dm",
        ),
        connected_platforms=[Platform.TELEGRAM],
        home_channels={},
    )
    dm_prompt = build_session_context_prompt(dm_context, redact_pii=True)

    assert "123456" not in dm_prompt
    assert '**Chat_ID:** `"8d969eef6eca"`' in dm_prompt


def test_topic_policy_is_frozen_for_existing_session(monkeypatch):
    class MetadataStore:
        def __init__(self):
            self.values = {}

        def get_session_metadata(self, session_key, key, default=None):
            return self.values.get((session_key, key), default)

        def set_session_metadata(self, session_key, key, value):
            self.values[(session_key, key)] = value
            return True

    monkeypatch.setattr(
        "agent.skill_commands.get_skill_commands",
        lambda: {"audit": {"name": "audit"}, "coding": {"name": "coding"}},
    )
    runner = GatewayRunner.__new__(GatewayRunner)
    runner.session_store = MetadataStore()
    first_config = _config(
        {
            "thread_id": "20",
            "enabled_skills": ["audit"],
            "enabled_toolsets": ["terminal"],
        }
    )
    first = runner._frozen_topic_policy(_source(), "session-key", first_config)

    changed_config = _config(
        {
            "thread_id": "20",
            "enabled_skills": ["coding"],
            "enabled_toolsets": [],
        }
    )
    resumed = runner._frozen_topic_policy(
        _source(), "session-key", changed_config
    )

    assert resumed == first
    assert resumed["enabled_skills"] == ["audit"]
    assert resumed["enabled_toolsets"] == ["terminal"]
