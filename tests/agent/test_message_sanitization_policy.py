"""Tests for the single-owner call_id + reasoning_content policies.

Audit F4 consolidation: agent/message_sanitization.py now owns the
deterministic call_id synthesis, call_id coalescing/dedup, and the
reasoning_content strip-vs-repad provider-direction policy. These tests pin
the owner functions' behavior (including byte-exact hash outputs — they feed
prompt-cache keys) and verify the legacy entry points still delegate here.
"""

from types import SimpleNamespace

import pytest

from agent.message_sanitization import (
    _MOA_REFERENCE_GUIDANCE_METADATA_KEY,
    apply_reasoning_content_policy,
    coalesce_tool_call_id,
    deterministic_call_id,
    matches_reasoning_echo_family,
    needs_reasoning_echo,
    prepare_reasoning_replay_messages_for_route,
    reapply_reasoning_echo,
    reasoning_replay_route_fingerprint,
    reasoning_echo_family,
    uniquify_tool_call_ids,
)


# ---------------------------------------------------------------------------
# deterministic_call_id — byte-exact (prompt-cache keys)
# ---------------------------------------------------------------------------

class TestDeterministicCallId:
    def test_known_hash_outputs_are_stable(self):
        # Golden values: sha256(f"{fn}:{args}:{index}")[:12] prefixed call_.
        # Any change here invalidates users' prompt caches — do NOT update
        # these expectations without a migration plan.
        assert deterministic_call_id("terminal", '{"command":"ls"}', 0) == \
            "call_40ccaef54d02"
        assert deterministic_call_id("terminal", '{"command":"ls"}', 1) == \
            "call_567cb168d22d"
        assert deterministic_call_id("", "", 0) == "call_feda901d71ea"

    def test_deterministic_across_calls(self):
        a = deterministic_call_id("web_search", '{"q":"x"}', 3)
        b = deterministic_call_id("web_search", '{"q":"x"}', 3)
        assert a == b
        assert a.startswith("call_")
        assert len(a) == len("call_") + 12

    def test_index_disambiguates(self):
        assert deterministic_call_id("t", "{}", 0) != deterministic_call_id("t", "{}", 1)

    def test_surrogates_do_not_crash(self):
        out = deterministic_call_id("t", "bad \ud800 arg", 0)
        assert out.startswith("call_")

    def test_codex_adapter_wrapper_delegates(self):
        from agent.codex_responses_adapter import _deterministic_call_id
        assert _deterministic_call_id("terminal", '{"command":"ls"}', 0) == \
            deterministic_call_id("terminal", '{"command":"ls"}', 0)

    def test_run_agent_static_delegates(self):
        from run_agent import AIAgent
        assert AIAgent._deterministic_call_id("terminal", '{"command":"ls"}', 0) == \
            deterministic_call_id("terminal", '{"command":"ls"}', 0)


# ---------------------------------------------------------------------------
# coalesce_tool_call_id
# ---------------------------------------------------------------------------

class TestCoalesceToolCallId:
    def test_dict_call_id_wins_over_id(self):
        assert coalesce_tool_call_id({"call_id": "c", "id": "i"}) == "c"

    def test_dict_falls_back_to_id_and_strips(self):
        assert coalesce_tool_call_id({"id": " i "}) == "i"
        assert coalesce_tool_call_id({"call_id": "", "id": "i2"}) == "i2"

    def test_dict_empty(self):
        assert coalesce_tool_call_id({}) == ""

    def test_object_forms(self):
        assert coalesce_tool_call_id(SimpleNamespace(call_id="c", id="i")) == "c"
        assert coalesce_tool_call_id(SimpleNamespace(call_id=None, id=" i ")) == "i"
        assert coalesce_tool_call_id(SimpleNamespace(call_id=None, id=None)) == ""

    def test_run_agent_static_delegates(self):
        from run_agent import AIAgent
        tc = {"call_id": "c9", "id": "i9"}
        assert AIAgent._get_tool_call_id_static(tc) == coalesce_tool_call_id(tc)


# ---------------------------------------------------------------------------
# uniquify_tool_call_ids
# ---------------------------------------------------------------------------

class TestUniquifyToolCallIds:
    def test_no_duplicates_untouched(self):
        tcs = [
            {"id": "a", "function": {"name": "f", "arguments": "{}"}},
            {"id": "b", "function": {"name": "g", "arguments": "{}"}},
        ]
        out = uniquify_tool_call_ids(tcs)
        assert out is tcs
        assert [tc["id"] for tc in out] == ["a", "b"]

    def test_duplicate_gets_deterministic_suffix(self):
        tcs = [
            {"id": "x", "call_id": "x", "function": {"name": "f", "arguments": "{}"}},
            {"id": "x", "call_id": "x", "function": {"name": "g", "arguments": "{}"}},
            {"id": "x", "function": {"name": "h", "arguments": "{}"}},
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[0]["id"] == "x"
        assert tcs[1]["id"] == "x_d2"
        assert tcs[1]["call_id"] == "x_d2"
        assert tcs[2]["id"] == "x_d3"

    def test_composite_id_collides_on_call_half_and_preserves_item_half(self):
        tcs = [
            {"id": "call_y|fc_1", "function": {"name": "f", "arguments": "{}"}},
            {"id": "call_y|fc_2", "function": {"name": "g", "arguments": "{}"}},
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[0]["id"] == "call_y|fc_1"
        assert tcs[1]["id"] == "call_y_d2|fc_2"

    def test_suffix_collision_advances_counter(self):
        tcs = [
            {"id": "z", "function": {"name": "a", "arguments": "{}"}},
            {"id": "z_d2", "function": {"name": "b", "arguments": "{}"}},
            {"id": "z", "function": {"name": "c", "arguments": "{}"}},
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[2]["id"] == "z_d3"

    def test_blank_and_non_string_ids_skipped(self):
        tcs = [
            {"id": "", "function": {"name": "a", "arguments": "{}"}},
            {"id": None, "function": {"name": "b", "arguments": "{}"}},
            SimpleNamespace(id=42, call_id=None, function=None),
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[0]["id"] == ""
        assert tcs[1]["id"] is None

    def test_namespace_objects_mutated(self):
        tcs = [
            SimpleNamespace(id="n", call_id="n",
                            function=SimpleNamespace(name="a", arguments="{}")),
            SimpleNamespace(id="n", call_id="n",
                            function=SimpleNamespace(name="b", arguments="{}")),
        ]
        uniquify_tool_call_ids(tcs)
        assert tcs[1].id == "n_d2"
        assert tcs[1].call_id == "n_d2"

    def test_empty_and_none_inputs(self):
        assert uniquify_tool_call_ids([]) == []
        assert uniquify_tool_call_ids(None) is None


# ---------------------------------------------------------------------------
# reasoning_echo_family — the provider-direction table
# ---------------------------------------------------------------------------

class TestReasoningEchoFamily:
    @pytest.mark.parametrize("provider,model,base_url,family", [
        ("kimi-coding", None, "https://x", "kimi"),
        ("kimi-coding-cn", None, "https://x", "kimi"),
        ("custom", None, "https://api.kimi.com/v1", "kimi"),
        ("custom", None, "https://api.moonshot.ai/v1", "kimi"),
        ("custom", None, "https://api.moonshot.cn/v1", "kimi"),
        ("deepseek", "whatever", "https://x", "deepseek"),
        ("DeepSeek", "whatever", "https://x", "deepseek"),
        ("openrouter", "deepseek/deepseek-v3", "https://openrouter.ai", "deepseek"),
        ("custom", None, "https://api.deepseek.com", "deepseek"),
        ("xiaomi", None, "https://x", "mimo"),
        ("custom", "MiMo-7B", "https://x", "mimo"),
        ("custom", None, "https://api.xiaomimimo.com/v1", "mimo"),
        ("openai", "gpt-5", "https://api.openai.com/v1", None),
        ("mistral", "mistral-large", "https://api.mistral.ai/v1", None),
        (None, None, None, None),
    ])
    def test_table(self, provider, model, base_url, family):
        assert reasoning_echo_family(provider, model, base_url) == family
        assert needs_reasoning_echo(provider, model, base_url) is (family is not None)

    def test_kimi_provider_match_is_exact_not_lowered(self):
        # Original predicate compared the raw provider string against the
        # kimi-coding set; keep that semantic.
        assert matches_reasoning_echo_family("kimi", "KIMI-CODING", None, "https://x") is False

    def test_membership_is_per_family(self):
        # A deepseek model pointed at a kimi host matches both families
        # independently (the per-family predicates on AIAgent rely on this).
        assert matches_reasoning_echo_family(
            "kimi", "custom", "deepseek-chat", "https://api.kimi.com") is True
        assert matches_reasoning_echo_family(
            "deepseek", "custom", "deepseek-chat", "https://api.kimi.com") is True

    def test_unknown_family_raises(self):
        with pytest.raises(KeyError):
            matches_reasoning_echo_family("nope", "p", "m", "https://x")


# ---------------------------------------------------------------------------
# apply_reasoning_content_policy
# ---------------------------------------------------------------------------

class TestApplyReasoningContentPolicy:
    def test_non_assistant_untouched(self):
        api = {"role": "user", "content": "u", "reasoning_content": "keep"}
        apply_reasoning_content_policy(
            {"role": "user", "content": "u", "reasoning_content": "keep"}, api, True)
        assert api["reasoning_content"] == "keep"

    def test_require_side_preserves_existing(self):
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": "thoughts"},
            api, True)
        assert api["reasoning_content"] == "thoughts"

    def test_require_side_upgrades_empty_string_to_space(self):
        api = {"role": "assistant", "content": "x", "reasoning_content": ""}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": ""}, api, True)
        assert api["reasoning_content"] == " "

    def test_strict_side_strips_existing(self):
        api = {"role": "assistant", "content": "x", "reasoning_content": " "}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": " "}, api, False)
        assert "reasoning_content" not in api

    @pytest.mark.parametrize(
        ("same_tool_turn", "reasoning_content", "expected"),
        [(True, "native thought", "native thought"), (False, "completed thought", None), (True, " ", None)],
    )
    def test_gemma4_only_preserves_explicit_current_tool_turn_reasoning(
        self, same_tool_turn, reasoning_content, expected
    ):
        api = {"role": "assistant", "reasoning_content": reasoning_content}
        origin = "route-primary"
        api["_reasoning_replay_origin"] = origin
        apply_reasoning_content_policy(dict(api), api, False, model="google/gemma-4-31b-it",
                                       preserve_explicit_reasoning=same_tool_turn,
                                       replay_origin=origin)
        if expected is None:
            assert "reasoning_content" not in api
        else:
            assert api["reasoning_content"] == expected

    def test_cross_provider_poisoned_history_pads_with_space(self):
        src = {"role": "assistant", "content": "x", "reasoning": "other-provider CoT",
               "tool_calls": [{"id": "c", "function": {"name": "t", "arguments": "{}"}}]}
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(src, api, True)
        assert api["reasoning_content"] == " "  # pad, never the foreign CoT

    def test_require_side_fallback_pads_foreign_marked_scratchpad(self):
        src = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "private Gemma scratchpad",
            "_reasoning_replay_origin": "gemma-route",
        }
        api = dict(src)

        apply_reasoning_content_policy(
            src,
            api,
            True,
            model="deepseek-reasoner",
            preserve_explicit_reasoning=True,
            replay_origin="deepseek-route",
        )

        assert api["reasoning_content"] == " "
        assert "_reasoning_replay_origin" not in api

    def test_reasoning_promoted_only_on_require_side(self):
        src = {"role": "assistant", "content": "x", "reasoning": "healthy"}
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy(src, api, True)
        assert api["reasoning_content"] == "healthy"
        api2 = {"role": "assistant", "content": "x", "reasoning_content": "stale"}
        apply_reasoning_content_policy(src, api2, False)
        assert "reasoning_content" not in api2

    def test_require_side_pads_bare_assistant_turn(self):
        api = {"role": "assistant", "content": "x"}
        apply_reasoning_content_policy({"role": "assistant", "content": "x"}, api, True)
        assert api["reasoning_content"] == " "

    def test_non_string_reasoning_content_removed(self):
        api = {"role": "assistant", "content": "x", "reasoning_content": None}
        apply_reasoning_content_policy(
            {"role": "assistant", "content": "x", "reasoning_content": None}, api, False)
        assert "reasoning_content" not in api


# ---------------------------------------------------------------------------
# reapply_reasoning_echo
# ---------------------------------------------------------------------------

class TestReapplyReasoningEcho:
    MSGS = [
        {"role": "assistant", "content": "a1", "reasoning_content": " "},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "u"},
        {"role": "tool", "content": "t", "tool_call_id": "c"},
    ]

    def test_require_side_pads_missing_only(self):
        import copy
        msgs = copy.deepcopy(self.MSGS)
        assert reapply_reasoning_echo(msgs, True) == 1
        assert msgs[0]["reasoning_content"] == " "  # untouched
        assert msgs[1]["reasoning_content"] == " "  # padded
        assert "reasoning_content" not in msgs[2]

    def test_strict_side_strips_all(self):
        import copy
        msgs = copy.deepcopy(self.MSGS)
        assert reapply_reasoning_echo(msgs, False) == 1
        assert all("reasoning_content" not in m for m in msgs)

    def test_gemma4_reapply_keeps_only_current_tool_turn_reasoning(self):
        current = {"role": "assistant", "tool_calls": [{}], "reasoning_content": "current"}
        current["_reasoning_replay_origin"] = "route-primary"
        apply_reasoning_content_policy(
            current,
            current,
            False,
            model="gemma4:31b",
            preserve_explicit_reasoning=True,
            replay_origin="route-primary",
        )
        msgs = [
            {"role": "assistant", "tool_calls": [{}], "reasoning_content": "old"},
            {"role": "user", "content": "current"},
            current,
            {"role": "assistant", "tool_calls": [{}], "reasoning_content": " "},
        ]
        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-primary",
        ) == 2
        assert "reasoning_content" not in msgs[0]
        assert msgs[2]["reasoning_content"] == "current"
        assert "reasoning_content" not in msgs[3]

    @pytest.mark.parametrize(
        "synthetic_flag",
        ["_empty_recovery_synthetic", "_kanban_stop_synthetic"],
    )
    def test_synthetic_user_nudge_does_not_end_gemma_tool_turn(self, synthetic_flag):
        current = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "current",
            "_reasoning_replay_origin": "route-primary",
        }
        msgs = [
            {"role": "user", "content": "current task"},
            current,
            {
                "role": "user",
                "content": "Continue processing after the empty response.",
                synthetic_flag: True,
            },
        ]

        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-primary",
        ) == 0
        assert current["reasoning_content"] == "current"
        assert current["_reasoning_replay_origin"] == "route-primary"

    def test_moa_reference_guidance_does_not_end_gemma_tool_turn(self):
        current = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "current",
            "_reasoning_replay_origin": "route-primary",
        }
        msgs = [
            {"role": "user", "content": "current task"},
            current,
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
            {
                "role": "user",
                "content": "[Mixture of Agents reference context]\nAdvice body.",
                _MOA_REFERENCE_GUIDANCE_METADATA_KEY: {"shape": "standalone"},
            },
        ]

        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-primary",
        ) == 0
        assert current["reasoning_content"] == "current"
        assert current["_reasoning_replay_origin"] == "route-primary"

    @pytest.mark.parametrize(
        "nudge_name",
        [
            "_LENGTH_CONTINUATION_OUTPUT_LIMIT",
            "_CODEX_INCOMPLETE_NUDGE",
            "_CODEX_ACK_CONTINUATION_NUDGE",
            "_EMPTY_TOOL_RESPONSE_NUDGE",
        ],
    )
    def test_moa_guidance_merged_into_synthetic_nudge_keeps_tool_turn_reasoning(
        self, nudge_name
    ):
        import agent.conversation_loop as conversation_loop

        current = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "current",
            "_reasoning_replay_origin": "route-primary",
        }
        nudge = getattr(conversation_loop, nudge_name)
        msgs = [
            {"role": "user", "content": "current task"},
            current,
            {
                "role": "user",
                "content": (
                    nudge
                    + "\n\n[Mixture of Agents reference context]\nAdvice body."
                ),
                _MOA_REFERENCE_GUIDANCE_METADATA_KEY: {
                    "shape": "string",
                    "boundary": len(nudge),
                },
            },
        ]

        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-primary",
        ) == 0
        assert current["reasoning_content"] == "current"
        assert current["_reasoning_replay_origin"] == "route-primary"

    def test_length_continuation_flag_survives_changed_content(self):
        current = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "current",
            "_reasoning_replay_origin": "route-primary",
        }
        msgs = [
            {"role": "user", "content": "current task"},
            current,
            {
                "role": "user",
                "content": "runtime-decorated continuation text",
                "_length_continuation_nudge": True,
            },
        ]

        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-primary",
        ) == 0
        assert current["reasoning_content"] == "current"

    def test_moa_guidance_keeps_image_only_user_turn_as_real_boundary(self):
        current = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "must be old after a real user turn",
            "_reasoning_replay_origin": "route-primary",
        }
        msgs = [
            current,
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
                    {
                        "type": "text",
                        "text": (
                            "\n\n[Mixture of Agents reference context]\nAdvice body."
                        ),
                    },
                ],
                _MOA_REFERENCE_GUIDANCE_METADATA_KEY: {
                    "shape": "list",
                    "boundary": 1,
                },
            },
        ]

        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-primary",
        ) == 1
        assert "reasoning_content" not in current

    def test_repeated_header_inside_guidance_does_not_create_user_boundary(self):
        from agent.moa_loop import _attach_reference_guidance

        current = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "current",
            "_reasoning_replay_origin": "route-primary",
        }
        msgs = [
            {"role": "user", "content": "current task"},
            current,
            {"role": "tool", "tool_call_id": "call_1", "content": "result"},
        ]
        _attach_reference_guidance(
            msgs,
            (
                "[Mixture of Agents reference context]\nAdvice quotes this:\n"
                "[Mixture of Agents reference context]\nNested example."
            ),
        )

        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-primary",
        ) == 0
        assert current["reasoning_content"] == "current"

    def test_user_authored_moa_header_remains_a_real_boundary(self):
        current = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "belongs to the previous turn",
            "_reasoning_replay_origin": "route-primary",
        }
        msgs = [
            {"role": "user", "content": "previous task"},
            current,
            {
                "role": "user",
                "content": "[Mixture of Agents reference context]\nPlease explain this block.",
            },
        ]

        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-primary",
        ) == 1
        assert "reasoning_content" not in current

    def test_fallback_to_gemma4_strips_foreign_reasoning(self):
        msgs = [
            {"role": "user", "content": "current"},
            {"role": "assistant", "tool_calls": [{}], "reasoning_content": "foreign"},
        ]

        assert reapply_reasoning_echo(
            msgs,
            False,
            model="gemma4:31b",
            replay_origin="route-fallback",
        ) == 1
        assert "reasoning_content" not in msgs[1]

    def test_require_side_fallback_replaces_foreign_gemma_reasoning(self):
        msgs = [
            {"role": "user", "content": "current"},
            {
                "role": "assistant",
                "tool_calls": [{}],
                "reasoning": "normalized copy must not cross routes",
                "reasoning_content": "gemma scratchpad",
                "_reasoning_replay_origin": "route-gemma",
            },
        ]

        assert reapply_reasoning_echo(
            msgs,
            True,
            model="deepseek-v4",
            replay_origin="route-deepseek",
        ) == 1
        assert msgs[1]["reasoning_content"] == " "
        assert "_reasoning_replay_origin" not in msgs[1]

    def test_same_model_different_endpoint_does_not_match_origin(self):
        source = {
            "role": "assistant",
            "tool_calls": [{}],
            "reasoning_content": "primary secret",
            "_reasoning_replay_origin": "route-TenantA",
        }
        api = dict(source)

        apply_reasoning_content_policy(
            source,
            api,
            False,
            model="Gemma4:Custom",
            preserve_explicit_reasoning=True,
            replay_origin="route-tenanta",
        )

        assert "reasoning_content" not in api
        assert "_reasoning_replay_origin" not in api

    def test_moa_uses_resolved_gemma4_aggregator_model(self):
        from agent.agent_runtime_helpers import reapply_reasoning_echo_for_provider

        agent = SimpleNamespace(
            provider="moa", model="closed",
            base_url="",
            client=SimpleNamespace(last_aggregator_slot={
                "provider": "custom",
                "base_url": "https://gemma.test/v1",
                "model": "google/gemma-4-31b-it",
            }),
            _needs_thinking_reasoning_pad=lambda: False,
        )
        source = {"role": "assistant", "tool_calls": [{}], "reasoning_content": "thought"}
        from agent.agent_runtime_helpers import _reasoning_replay_route
        _, source["_reasoning_replay_origin"] = _reasoning_replay_route(agent)
        current = dict(source)
        from agent.agent_runtime_helpers import copy_reasoning_content_for_api
        copy_reasoning_content_for_api(
            agent, source, current, preserve_explicit_reasoning=True
        )
        msgs = [{"role": "user", "content": "current"}, current]

        assert reapply_reasoning_echo_for_provider(agent, msgs) == 0
        assert msgs[1]["reasoning_content"] == "thought"

    def test_moa_uses_actual_successful_fallback_route(self):
        from agent.agent_runtime_helpers import _reasoning_replay_route

        fallback_route = {
            "provider": "openai",
            "base_url": "https://fallback.test/v1",
            "model": "fallback-model",
        }
        moa_agent = SimpleNamespace(
            provider="moa",
            model="closed",
            base_url="",
            client=SimpleNamespace(
                last_aggregator_slot={
                    "provider": "custom",
                    "base_url": "https://gemma.test/v1",
                    "model": "google/gemma-4-31b-it",
                },
                last_aggregator_runtime=fallback_route,
            ),
        )
        direct_agent = SimpleNamespace(**fallback_route)

        model, fingerprint = _reasoning_replay_route(moa_agent)
        _, expected_fingerprint = _reasoning_replay_route(direct_agent)

        assert model == "fallback-model"
        assert fingerprint == expected_fingerprint

    def test_auxiliary_wire_shapes_marked_reasoning_for_concrete_route(self):
        gemma_route = {
            "provider": "openrouter",
            "base_url": "https://gemma.test/v1",
            "model": "google/gemma-4-31b-it",
        }
        origin = reasoning_replay_route_fingerprint(**gemma_route)
        source = [
            {"role": "user", "content": "current"},
            {
                "role": "assistant",
                "tool_calls": [{}],
                "reasoning_content": "private scratchpad",
                "_reasoning_replay_origin": origin,
            },
        ]

        same_route = prepare_reasoning_replay_messages_for_route(
            source, **gemma_route
        )
        strict_route = prepare_reasoning_replay_messages_for_route(
            source,
            provider="openai",
            base_url="https://strict.test/v1",
            model="strict-model",
        )
        require_side_route = prepare_reasoning_replay_messages_for_route(
            source,
            provider="deepseek",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-reasoner",
        )

        assert same_route[1]["reasoning_content"] == "private scratchpad"
        assert "reasoning_content" not in strict_route[1]
        assert require_side_route[1]["reasoning_content"] == " "
        assert all("_reasoning_replay_origin" not in rows[1] for rows in (
            same_route, strict_route, require_side_route
        ))
        assert source[1]["_reasoning_replay_origin"] == origin

    def test_auxiliary_wire_strips_moa_guidance_provenance(self):
        source = [
            {
                "role": "user",
                "content": "task\n\n[Mixture of Agents reference context]\nAdvice.",
                _MOA_REFERENCE_GUIDANCE_METADATA_KEY: {
                    "shape": "string",
                    "boundary": len("task"),
                },
            },
        ]

        for route in (
            {
                "provider": "openrouter",
                "base_url": "https://openrouter.ai/api/v1",
                "model": "google/gemma-4-31b-it",
            },
            {
                "provider": "deepseek",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-reasoner",
            },
        ):
            wire = prepare_reasoning_replay_messages_for_route(source, **route)
            assert _MOA_REFERENCE_GUIDANCE_METADATA_KEY not in wire[0]
            assert "reasoning_content" not in wire[0]
        assert source[0][_MOA_REFERENCE_GUIDANCE_METADATA_KEY]["shape"] == "string"

    def test_idempotent(self):
        import copy
        msgs = copy.deepcopy(self.MSGS)
        reapply_reasoning_echo(msgs, True)
        assert reapply_reasoning_echo(msgs, True) == 0
        reapply_reasoning_echo(msgs, False)
        assert reapply_reasoning_echo(msgs, False) == 0
