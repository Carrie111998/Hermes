# ================================
# 🧰 ENV & PERFORMANCE — BEGIN
# ================================
from __future__ import annotations

from types import SimpleNamespace

from agent import skill_router
# ================================
# 🧰 ENV & PERFORMANCE — END
# ================================


# ================================
# 📊 ADVANCED DIAGNOSTICS — BEGIN
# ================================
SKILLS = [
    skill_router.RouterSkill("hot", "core", "Always available"),
    skill_router.RouterSkill("debug", "software-development", "Debug failures"),
    skill_router.RouterSkill("deploy", "devops", "Deploy services"),
]


def _config(**overrides):
    config = {
        "enabled": True,
        "hot_skills": ["hot"],
        "max_families": 3,
        "max_exact_lookups": 3,
        "max_ranked": 5,
        "timeout_seconds": 60,
        "provider": "",
        "model": "",
    }
    config.update(overrides)
    return config


def _state(**config_overrides):
    return skill_router.build_staged_router_session_state(
        {
            "core": [("hot", "Always available")],
            "software-development": [("debug", "Debug failures")],
            "devops": [("deploy", "Deploy services")],
        },
        _config(**config_overrides),
    )


def test_disabled_router_makes_no_catalog_or_model_call(monkeypatch):
    monkeypatch.setattr(
        skill_router,
        "collect_router_skills",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("catalog scanned")),
    )

    assert skill_router.route_skills_for_turn(SimpleNamespace(), "debug it") == ""


def test_three_stage_router_keeps_only_exact_allowed_names(monkeypatch):
    monkeypatch.setattr(
        skill_router,
        "collect_router_skills",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("live catalog used")),
    )
    responses = iter(
        [
            '{"families":["software-development"],"exact_skills":["invented"]}',
            '{"ranking":["debug","invented","hot"]}',
            '{"ranking":["debug","invented"]}',
        ]
    )
    calls = []

    def fake_call(_agent, prompt, _config):
        calls.append(prompt)
        return next(responses)

    monkeypatch.setattr(skill_router, "_call_router_model", fake_call)

    note = skill_router.route_skills_for_turn(
        SimpleNamespace(
            _staged_skill_router_state=_state(),
            valid_tool_names=set(),
            enabled_toolsets=[],
            provider="p",
            model="m",
        ),
        "find the bug",
    )

    assert len(calls) == 3
    assert "- debug: Debug failures" in note
    assert "invented" not in note
    assert "- hot:" not in note


def test_router_failure_exposes_full_turn_catalog(monkeypatch):
    monkeypatch.setattr(
        skill_router,
        "collect_router_skills",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("live catalog used")),
    )
    monkeypatch.setattr(
        skill_router,
        "_call_router_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )

    note = skill_router.route_skills_for_turn(
        SimpleNamespace(_staged_skill_router_state=_state()), "deploy it"
    )

    assert note.startswith("[SKILL ROUTER FALLBACK")
    assert all(skill.name in note for skill in SKILLS)


def test_staged_index_contains_hot_descriptions_and_family_counts_only():
    lines = skill_router.staged_index_lines(
        {
            "core": [("hot", "Always available")],
            "devops/k8s": [("deploy", "Deploy services")],
            "devops/infra": [("other", "Other operation")],
        },
        _config(),
    )
    rendered = "\n".join(lines)

    assert "hot: Always available" in rendered
    assert "devops: 2 skills" in rendered
    assert "Deploy services" not in rendered
    assert "Other operation" not in rendered


def test_plain_prompt_marker_cannot_activate_router(monkeypatch):
    monkeypatch.setattr(
        skill_router,
        "_call_router_model",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model called")),
    )
    agent = SimpleNamespace(_staged_skill_router_config=_config())

    note = skill_router.route_skills_for_turn(
        agent, skill_router.STAGED_ROUTER_PROMPT_MARKER
    )

    assert note == ""


def test_session_state_captures_exact_prompt_visible_catalog():
    state = skill_router.build_staged_router_session_state(
        {"visible": [("one", "Shown")], "empty": []}, _config(hot_skills=["one"])
    )

    assert state["enabled"] is True
    assert state["config"]["hot_skills"] == ["one"]
    assert state["skills"] == [
        {"name": "one", "category": "visible", "description": "Shown"}
    ]


def test_resumed_session_restores_persisted_structured_router_state():
    persisted = _state(hot_skills=["debug"])
    agent = SimpleNamespace(
        session_id="resumed",
        _session_db=SimpleNamespace(
            get_session=lambda _sid: {
                "system_prompt": "persisted prompt",
                "model_config": {"staged_skill_router": persisted},
            }
        ),
        _session_init_model_config={"max_tokens": 1},
    )

    skill_router.bind_staged_router_session_state(agent)

    assert agent._staged_skill_router_state == persisted
    assert agent._staged_skill_router_state_bound is True
    assert agent._session_init_model_config["staged_skill_router"] == persisted


def test_old_session_without_structured_state_stays_disabled_despite_marker():
    agent = SimpleNamespace(
        session_id="old",
        _session_db=SimpleNamespace(
            get_session=lambda _sid: {
                "system_prompt": skill_router.STAGED_ROUTER_PROMPT_MARKER,
                "model_config": {},
            }
        ),
        _session_init_model_config={},
    )

    skill_router.bind_staged_router_session_state(agent)

    assert agent._staged_skill_router_state["enabled"] is False
    assert skill_router.route_skills_for_turn(agent, "debug it") == ""


# ================================
# 📊 ADVANCED DIAGNOSTICS — END
# ================================
