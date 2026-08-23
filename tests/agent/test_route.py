"""Tests for the Hermes-native per-turn router."""

from pathlib import Path
from types import SimpleNamespace
import json

from agent import route


def _write_policy_tree(root: Path) -> None:
    (root / "route-policy.json").write_text(
        '{"level_workflows":{"L0":"direct","L1":"standard","L2":"controlled-execution","L3":"expert-reviewed"},'
        '"level_contracts":{"L0":"fast-economy","L1":"standard-reasoning","L2":"complex-execution","L3":"high-stakes-decision"},'
        '"specialized_workflows":{'
        '"local_deterministic":{"level":"L0","risk":"low","workflow":"local-deterministic","contract":"local-deterministic"},'
        '"local_visual_extraction":{"level":"L0","risk":"low","workflow":"local-visual-extraction","contract":"local-deterministic"}}}',
        encoding="utf-8",
    )
    (root / "capability-contracts.json").write_text(
        '{"contracts":{"local-deterministic":{"tools":"none","reasoning_intent":"off"},"fast-economy":{},"standard-reasoning":{},"complex-execution":{},"high-stakes-decision":{}}}',
        encoding="utf-8",
    )
    (root / "model-policies.json").write_text(
        '{"policies":{'
        '"local-deterministic":{"primary":"ollama/qwen3-vl:2b"},'
        '"fast-economy":{"primary":"opencode-go/fast","failover":["deepseek/flash"]},'
        '"standard-reasoning":{"primary":"openai-codex/standard","failover":["deepseek/flash"]},'
        '"complex-execution":{"primary":"opencode-go/pro","failover":["openai-codex/standard"]},'
        '"high-stakes-decision":{"primary":"opencode-go/pro","failover":["deepseek/pro"]},'
        '"multimodal-extraction":{"primary":"bboluo/[L]gemini-3.1-pro-preview",'
        '"soft_failover":["minimax-cn/MiniMax-M3","openai-codex/gpt-5.6-terra","openai-codex/gpt-5.6-sol"],'
        '"hard_failover":[]},'
        '"independent-review":{"primary":"dynamic","rules":{"deepseek":["openai-codex/reviewer"]}}'
        '}}',
        encoding="utf-8",
    )
    (root / "model-profiles.json").write_text(
        '{"profiles":{"opencode-go/pro":{"vendor_family":"deepseek"},'
        '"ollama/qwen3-vl:2b":{"vendor_family":"local","supports_vision":true,"context_window":262144,'
        '"supported_reasoning":["off"],"thinking_map":{"off":"off"}},'
        '"openai-codex/reviewer":{"vendor_family":"openai"}}}',
        encoding="utf-8",
    )
    (root / "execution-roles.json").write_text(
        '{"roles":{"local-worker":{"model_policy":"local-deterministic"},'
        '"local-extractor":{"model_policy":"local-deterministic"}}}',
        encoding="utf-8",
    )
    (root / "workflow-templates.json").write_text(
        '{"templates":{"local-deterministic":{"roles":["local-worker"]},'
        '"local-visual-extraction":{"roles":["local-extractor"]}}}',
        encoding="utf-8",
    )


def test_classify_turn_has_route_risk_ladder():
    route_policy = {
        "level_workflows": {
            "L0": "direct",
            "L1": "standard",
            "L2": "controlled-execution",
            "L3": "expert-reviewed",
        }
    }
    assert route.classify_turn("translate this", route_policy)[0] == "L0"
    assert route.classify_turn("explain this concept", route_policy)[0] == "L1"
    assert route.classify_turn("modify the configuration file", route_policy)[0] == "L2"
    assert route.classify_turn("review this investment decision", route_policy)[:2] == ("L3", "high")


def test_standalone_file_edit_uses_l1_bounded_worker_path():
    route_policy = {
        "level_workflows": {
            "L0": "direct",
            "L1": "standard",
            "L2": "controlled-execution",
            "L3": "expert-reviewed",
        }
    }
    assert route.classify_turn(
        "修改这个捷克收益测算.xlsx里的敏感性分析和描述",
        route_policy,
    ) == ("L1", "low", "file-edit")


def test_classify_turn_explanatory_finance_question_stays_l1():
    """A pure 'why' / IRR-explanation question must NOT escalate to L2/L3 DAG
    just because it mentions investment/project/analysis (regression for the
    empty-response bug where the DAG's auxiliary models all failed)."""
    route_policy = {
        "level_workflows": {
            "L0": "direct",
            "L1": "standard",
            "L2": "controlled-execution",
            "L3": "expert-reviewed",
        }
    }
    cases = [
        "为什么项目IRR要高于我方全投资IRR，应该是我方的全投资IRR高于项目才对",
        "解释一下什么是优先股和劣后级的区别",
        "为什么汇率波动会影响项目收益，怎么理解这个口径",
    ]
    for case in cases:
        assert route.classify_turn(case, route_policy)[0] == "L1", case


def test_investment_risk_decision_requires_high_risk_route():
    route_policy = {
        "level_workflows": {
            "L0": "direct",
            "L1": "standard",
            "L2": "controlled-execution",
            "L3": "expert-reviewed",
        }
    }
    assert route.classify_turn(
        "这个投资方案合理吗？请分析一下风险", route_policy
    )[:2] == ("L3", "high")


def test_classify_turn_explanatory_with_execution_intent_still_escalates():
    """Explanatory phrasing WITH an execution verb must still route up —
    'why ... then delete the file' is a real L2/L3 instruction, not a chat."""
    route_policy = {
        "level_workflows": {
            "L0": "direct",
            "L1": "standard",
            "L2": "controlled-execution",
            "L3": "expert-reviewed",
        }
    }
    assert route.classify_turn(
        "为什么代码报错了，帮我修改配置文件修复它", route_policy
    )[0] == "L2"
    assert route.classify_turn(
        "为什么系统有问题，请部署新的配置", route_policy
    )[0] == "L2"


def test_explanatory_question_skips_semantic_router():
    """Pure 'why' questions must bypass the LLM semantic router entirely —
    otherwise finance words ('投资/IRR') get re-classified as L3 high-risk and
    the turn spins up a DAG that can block and return empty (the reported bug)."""
    route_policy = {
        "semantic_router": {
            "enabled": True,
            "triggers": ["multimodal", "high_risk", "uncertain"],
        }
    }
    cases = [
        "为什么项目IRR要高于我方全投资IRR，应该是我方的全投资IRR高于项目才对",
        "解释一下什么是优先股和劣后级的区别",
    ]
    for case in cases:
        assert not route._should_use_semantic_router(case, route_policy), case


def test_execution_intent_still_triggers_semantic_router():
    """A high-risk execution request must still go through the semantic router."""
    route_policy = {
        "semantic_router": {
            "enabled": True,
            "triggers": ["high_risk"],
        }
    }
    assert route._should_use_semantic_router(
        "为什么有漏洞，帮我删除这个文件并部署新的", route_policy
    )


def test_apply_route_switches_once_and_installs_ordered_fallbacks(tmp_path, monkeypatch):
    _write_policy_tree(tmp_path)
    config = {"routing": {"enabled": True, "source_dir": str(tmp_path)}}

    class FakeAgent:
        provider = "openai-codex"
        model = "old"
        _route_disable_tools = True

        def __init__(self):
            self.switches = []

        def switch_model(self, model, provider, api_key, base_url, api_mode):
            self.switches.append((model, provider, api_key, base_url, api_mode))
            self.model = model
            self.provider = provider

    agent = FakeAgent()
    monkeypatch.setattr(
        route,
        "_runtime_for",
        lambda candidate: {
            "provider": candidate["provider"],
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "api_mode": "chat_completions",
        },
    )

    decision = route.apply_route(agent, "modify the configuration file", config)

    assert decision is not None
    assert decision.level == "L2"
    assert decision.selected == {"provider": "opencode-go", "model": "pro"}
    assert agent.switches == [
        ("pro", "opencode-go", "test-key", "https://example.invalid/v1", "chat_completions")
    ]
    assert agent._fallback_chain == [{"provider": "openai-codex", "model": "standard"}]
    assert agent._fallback_index == 0
    assert agent._fallback_activated is False
    assert agent._route_disable_tools is False

    # Cached gateway agents must not carry the coordinated-workflow guard into
    # the next ordinary turn.
    route.apply_route(agent, "explain this concept", config)
    assert agent._direct_execution_guard is False
    assert agent._direct_execution_guard_tools == set()


def test_active_runtime_snapshot_is_the_route_policy_source(tmp_path, monkeypatch):
    _write_policy_tree(tmp_path)
    snapshot_bundle = {
        "route_policy": json.loads((tmp_path / "route-policy.json").read_text()),
        "capability_contracts": json.loads(
            (tmp_path / "capability-contracts.json").read_text()
        ),
        "model_policies": json.loads((tmp_path / "model-policies.json").read_text()),
        "execution_roles": json.loads(
            (tmp_path / "execution-roles.json").read_text()
        ),
        "model_profiles": json.loads((tmp_path / "model-profiles.json").read_text()),
    }
    snapshot_bundle["model_policies"]["policies"]["complex-execution"]["primary"] = (
        "opencode-go/snapshot-pro"
    )

    class FakeAgent:
        provider = "openai-codex"
        model = "old"
        runtime_registry = SimpleNamespace(status="active")
        runtime_snapshot = SimpleNamespace(bundle=snapshot_bundle)

        def __init__(self):
            self.switches = []

        def switch_model(self, model, provider, api_key, base_url, api_mode):
            self.switches.append((model, provider))
            self.model = model
            self.provider = provider

    agent = FakeAgent()
    monkeypatch.setattr(
        route,
        "_runtime_for",
        lambda candidate: {
            "provider": candidate["provider"],
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "api_mode": "chat_completions",
        },
    )

    decision = route.apply_route(
        agent,
        "modify the configuration file",
        {"routing": {"enabled": True, "source_dir": str(tmp_path)}},
    )

    assert decision is not None
    assert decision.selected == {"provider": "opencode-go", "model": "snapshot-pro"}


def test_inactive_registry_blocks_controlled_route(tmp_path):
    _write_policy_tree(tmp_path)

    class FakeAgent:
        runtime_registry = SimpleNamespace(status="inactive")
        runtime_snapshot = None

    try:
        route.apply_route(
            FakeAgent(),
            "modify the configuration file",
            {"routing": {"enabled": True, "source_dir": str(tmp_path)}},
        )
    except route.RoutingBlockedError as exc:
        assert exc.reason == "runtime_registry_inactive"
    else:
        raise AssertionError("inactive registry must block controlled routes")


def test_inactive_registry_blocks_even_direct_route(tmp_path):
    _write_policy_tree(tmp_path)

    class FakeAgent:
        runtime_registry = SimpleNamespace(status="inactive")
        runtime_snapshot = None

    try:
        route.apply_route(
            FakeAgent(),
            "explain this concept",
            {"routing": {"enabled": True, "source_dir": str(tmp_path)}},
        )
    except route.RoutingBlockedError as exc:
        assert exc.reason == "runtime_registry_inactive"
    else:
        raise AssertionError("inactive registry must block every routed turn")


def test_route_candidate_exhaustion_does_not_fall_back_to_native_model(tmp_path, monkeypatch):
    _write_policy_tree(tmp_path)

    class FakeAgent:
        provider = "openai-codex"
        model = "native"

    monkeypatch.setattr(
        route,
        "_runtime_for",
        lambda _candidate: (_ for _ in ()).throw(RuntimeError("unavailable")),
    )
    try:
        route.apply_route(
            FakeAgent(),
            "explain this concept",
            {"routing": {"enabled": True, "source_dir": str(tmp_path)}},
        )
    except route.RoutingBlockedError as exc:
        assert exc.reason == "route_candidates_unavailable"
    else:
        raise AssertionError("route exhaustion must not use the native model")


def test_disabled_router_is_a_noop(tmp_path):
    _write_policy_tree(tmp_path)

    class FakeAgent:
        provider = "openai-codex"
        model = "old"



def test_l3_review_uses_cross_vendor_reviewer(tmp_path, monkeypatch):
    _write_policy_tree(tmp_path)
    config = {
        "routing": {
            "enabled": True,
            "source_dir": str(tmp_path),
            "review": {"enabled": True, "fail_closed": True},
        }
    }

    from agent import auxiliary_client

    seen = {}

    def fake_call_llm(**kwargs):
        seen["provider"] = kwargs["provider"]
        seen["model"] = kwargs["model"]
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="PASS\nIndependent evidence is sufficient."))]
        )

    monkeypatch.setattr(auxiliary_client, "call_llm", fake_call_llm)
    evidence = route.review_high_risk_result(
        {
            "level": "L3",
            "selected": {"provider": "opencode-go", "model": "pro"},
        },
        user_message="Review an investment decision.",
        answer="Candidate answer.",
        config=config,
    )

    assert evidence is not None
    assert evidence["status"] == "passed"
    assert evidence["verdict"] == "PASS"
    assert seen == {"provider": "openai-codex", "model": "reviewer"}


def test_l3_review_blocks_when_verdict_is_not_parseable(tmp_path, monkeypatch):
    _write_policy_tree(tmp_path)
    config = {"routing": {"enabled": True, "source_dir": str(tmp_path), "review": {"enabled": True}}}

    from agent import auxiliary_client

    monkeypatch.setattr(
        auxiliary_client,
        "call_llm",
        lambda **_kwargs: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Looks good"))]),
    )
    evidence = route.review_high_risk_result(
        {"level": "L3", "selected": {"provider": "opencode-go", "model": "pro"}},
        user_message="Review an investment decision.",
        answer="Candidate answer.",
        config=config,
    )

def test_l3_review_fail_closed_blocks_delivery(tmp_path, monkeypatch):
    """The conversation-loop gate: a blocked review must replace the final
    response when fail_closed=true (Hermes contract), not silently deliver it."""
    _write_policy_tree(tmp_path)
    config = {"routing": {"enabled": True, "source_dir": str(tmp_path), "review": {"enabled": True, "fail_closed": True}}}

    from agent import auxiliary_client

    monkeypatch.setattr(
        auxiliary_client,
        "call_llm",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("reviewer provider unavailable")),
    )
    evidence = route.review_high_risk_result(
        {"level": "L3", "selected": {"provider": "opencode-go", "model": "pro"}},
        user_message="Review an investment decision.",
        answer="Candidate answer.",
        config=config,
    )

    assert evidence is not None
    assert evidence["status"] == "blocked"
    assert evidence["reason"] == "cross_vendor_review_unavailable"
    assert route.review_fail_closed(config) is True


def test_l3_review_fail_open_allows_delivery(tmp_path, monkeypatch):
    """With fail_closed=false the gate records evidence but does not block."""
    _write_policy_tree(tmp_path)
    config = {"routing": {"enabled": True, "source_dir": str(tmp_path), "review": {"enabled": True, "fail_closed": False}}}

    from agent import auxiliary_client

    monkeypatch.setattr(
        auxiliary_client,
        "call_llm",
        lambda **_kwargs: SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="REVISE\nFix the numbers."))]),
    )
    evidence = route.review_high_risk_result(
        {"level": "L3", "selected": {"provider": "opencode-go", "model": "pro"}},
        user_message="Review an investment decision.",
        answer="Candidate answer.",
        config=config,
    )

    assert evidence is not None
    assert evidence["status"] == "revise"
    assert route.review_fail_closed(config) is False


def test_primary_pool_precedes_role_failover(tmp_path):
    """Flexible primary pools stay ahead of GPT soft and DeepSeek hard tiers."""
    _write_policy_tree(tmp_path)
    policies = {
        "policies": {
            "standard-reasoning": {
                "primary_pool": ["minimax-cn/MiniMax-M3"],
                "soft_failover": ["openai-codex/gpt-5.6-luna"],
                "hard_failover": ["deepseek/deepseek-v4-flash"],
            }
        }
    }
    import json
    (tmp_path / "model-policies.json").write_text(json.dumps(policies), encoding="utf-8")
    assert route._candidate_ids(policies["policies"]["standard-reasoning"]) == [
        "minimax-cn/MiniMax-M3",
        "openai-codex/gpt-5.6-luna",
        "deepseek/deepseek-v4-flash",
    ]


def test_multimodal_exception_order_is_gemini_minimax_gpt():
    """Raw-image routing uses the sole Gemini -> MiniMax -> GPT exception."""
    policy = {
        "primary": "bboluo/[L]gemini-3.1-pro-preview",
        "soft_failover": [
            "minimax-cn/MiniMax-M3",
            "openai-codex/gpt-5.6-terra",
        ],
        "hard_failover": [],
    }
    assert route._candidate_ids(policy) == [
        "bboluo/[L]gemini-3.1-pro-preview",
        "minimax-cn/MiniMax-M3",
        "openai-codex/gpt-5.6-terra",
    ]


def test_explicit_local_text_workflow_switches_runtime_to_ollama(tmp_path, monkeypatch):
    _write_policy_tree(tmp_path)
    config = {"routing": {"enabled": True, "source_dir": str(tmp_path)}}

    class FakeAgent:
        provider = "openai-codex"
        model = "standard"

        def __init__(self):
            self._workflow_roles = []
            self._fallback_chain = []
            self._route_disable_tools = False
            self._ollama_num_ctx = None
            self.reasoning_effort = "max"
            self.reasoning_config = {"enabled": True, "effort": "max"}

        def switch_model(self, model, provider, api_key, base_url, api_mode):
            self.model = model
            self.provider = provider

    monkeypatch.setattr(
        route,
        "_runtime_for",
        lambda candidate: {
            "provider": candidate["provider"],
            "api_key": "test-key",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_mode": "chat_completions",
        },
    )

    agent = FakeAgent()
    decision = route.apply_route(
        agent,
        "workflow: local_deterministic\nReturn exactly LOCAL-TEXT.",
        config,
    )

    assert decision is not None
    assert decision.workflow == "local-deterministic"
    assert decision.model_policy == "local-deterministic"
    assert decision.selected == {"provider": "ollama", "model": "qwen3-vl:2b"}
    assert agent._workflow_roles == ["local-worker"]
    assert agent._fallback_chain == []
    assert agent._route_disable_tools is True
    assert agent._ollama_num_ctx == 262144
    assert agent.reasoning_effort == "off"
    assert agent.reasoning_config == {"enabled": False}
    assert route.specialized_system_message(decision) == (
        "You are a local deterministic worker. Follow the user's request exactly. "
        "Do not call tools. Return only the requested result."
    )

    agent._cached_system_prompt = route.specialized_system_message(decision)
    route.apply_route(agent, "explain this concept", config)
    assert agent._cached_system_prompt is None


def test_release_gate_blocks_unreviewed_delivery(monkeypatch):
    class FakeAgent:
        _route_decision = {
            "level": "L3",
            "selected": {"provider": "opencode-go", "model": "pro"},
        }
        _workflow_review = True
        _workflow_verify = False
        _route_policy_bundle = {}
        _route_config = {"routing": {"review": {"fail_closed": True}}}

    monkeypatch.setattr(
        route,
        "review_high_risk_result",
        lambda *_args, **_kwargs: {"status": "blocked", "reason": "review_down"},
    )
    answer = route.enforce_release_gates(
        FakeAgent(), user_message="Review this investment decision.", answer="Candidate"
    )
    assert answer.startswith("Delivery blocked:")


def test_semantic_local_visual_workflow_keeps_image_for_ollama(tmp_path, monkeypatch):
    _write_policy_tree(tmp_path)
    config = {"routing": {"enabled": True, "source_dir": str(tmp_path)}}
    image = tmp_path / "smoke.png"
    image.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d494844520000000100000001080600000"
            "01f15c4890000000a49444154789c6360000002000100ffff0300000600"
            "0557bfabd40000000049454e44ae426082"
        )
    )

    class FakeAgent:
        provider = "openai-codex"
        model = "standard"

        def __init__(self):
            self._workflow_roles = []

        def switch_model(self, model, provider, api_key, base_url, api_mode):
            self.model = model
            self.provider = provider

    monkeypatch.setattr(route, "_should_use_semantic_router", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        route,
        "_semantic_classify",
        lambda *_args, **_kwargs: ("L0", "low", "local_visual_extraction"),
    )
    monkeypatch.setattr(
        route,
        "_runtime_for",
        lambda candidate: {
            "provider": candidate["provider"],
            "api_key": "test-key",
            "base_url": "http://127.0.0.1:11434/v1",
            "api_mode": "chat_completions",
        },
    )

    message = f"Extract the visible text. image_url: {image}]"
    agent = FakeAgent()
    decision = route.apply_route(agent, message, config)
    prepared = route.prepare_specialized_message(message, decision)

    assert decision is not None
    assert decision.workflow == "local-visual-extraction"
    assert decision.model_policy == "local-deterministic"
    assert decision.selected == {"provider": "ollama", "model": "qwen3-vl:2b"}
    assert agent._workflow_roles == ["local-extractor"]
    assert any(part.get("type") == "image_url" for part in prepared)
    text_part = next(part for part in prepared if part.get("type") == "text")
    assert str(image) in text_part["text"]


def test_local_execution_roles_resolve_ollama_candidate(tmp_path):
    _write_policy_tree(tmp_path)
    config = {"routing": {"source_dir": str(tmp_path)}}

    expected = [{"provider": "ollama", "model": "qwen3-vl:2b"}]
    assert route.get_role_candidates("local-worker", config) == expected
    assert route.get_role_candidates("local-extractor", config) == expected


def test_local_policy_does_not_change_cloud_multimodal_chain(tmp_path):
    _write_policy_tree(tmp_path)
    model_policies = route._read_json(tmp_path / "model-policies.json")["policies"]

    assert route._candidate_ids(model_policies["multimodal-extraction"]) == [
        "bboluo/[L]gemini-3.1-pro-preview",
        "minimax-cn/MiniMax-M3",
        "openai-codex/gpt-5.6-terra",
        "openai-codex/gpt-5.6-sol",
    ]
    assert route._candidate_ids(model_policies["local-deterministic"]) == [
        "ollama/qwen3-vl:2b"
    ]


def test_apply_route_resolves_and_resets_output_policy_per_turn(tmp_path, monkeypatch):
    _write_policy_tree(tmp_path)
    contracts_path = tmp_path / "capability-contracts.json"
    contracts = json.loads(contracts_path.read_text(encoding="utf-8"))
    contracts["contracts"]["fast-economy"]["output_policy"] = "concise_evidence"
    contracts["contracts"]["standard-reasoning"]["output_policy"] = "standard"
    contracts["contracts"]["complex-execution"]["output_policy"] = "concise_evidence"
    contracts["contracts"]["high-stakes-decision"]["output_policy"] = "concise_evidence"
    contracts_path.write_text(json.dumps(contracts), encoding="utf-8")

    class FakeAgent:
        provider = "openai-codex"
        model = "old"

        def switch_model(self, model, provider, api_key, base_url, api_mode):
            self.model = model
            self.provider = provider

    monkeypatch.setattr(
        route,
        "_runtime_for",
        lambda candidate: {
            "provider": candidate["provider"],
            "api_key": "test-key",
            "base_url": "https://example.invalid/v1",
            "api_mode": "chat_completions",
        },
    )
    agent = FakeAgent()
    config = {"routing": {"enabled": True, "source_dir": str(tmp_path)}}

    route.apply_route(agent, "translate this", config)
    assert agent._route_output_policy == "concise_evidence"

    route.apply_route(agent, "explain this concept", config)
    assert agent._route_output_policy == "standard"

    route.apply_route(agent, "modify the configuration file", config)
    assert agent._route_output_policy == "full_evidence"

    route.apply_route(agent, "review this investment decision", config)
    assert agent._route_output_policy == "full_evidence"


def test_reviewer_and_publishing_contracts_cannot_reduce_output_evidence():
    assert route.resolve_output_policy("independent-review", {"output_policy": "concise_evidence"}) == "full_evidence"
    assert route.resolve_output_policy("publishing", {"output_policy": "concise_evidence"}) == "full_evidence"
