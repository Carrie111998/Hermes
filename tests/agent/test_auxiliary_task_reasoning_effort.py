"""Per-task reasoning-effort config on auxiliary calls.

auxiliary.<task>.reasoning_effort lets operators disable (or pin) thinking on
a single aux task without touching the main model. Canonical case: compression
against a local thinking model (Ollama + gemma), where reasoning burns the
whole output budget before the first summary character and compaction loops on
empty summaries.

Wire contract (probe-verified against Ollama /v1/chat/completions,
gemma4:26b-mlx, 2026-08-23):
  - disabled → top-level ``reasoning_effort: "none"`` IS honored (12.5s, 763
    tok, 0 thinking chars vs 72s / 4.4K tok / 11K thinking chars without);
    ``extra_body.reasoning = {enabled: false}`` alone is a silent no-op.
  - The custom/Ollama provider profile maps disabled → top-level
    ``reasoning_effort="none"`` + ``extra_body.think=False`` (ollama#14820).
  - Providers with NO reasoning profile get the top-level key from the
    generic fallback in _build_call_kwargs (config-gated only).
"""

from unittest.mock import patch

from agent.auxiliary_client import _build_call_kwargs


def _msgs():
    return [{"role": "user", "content": "x"}]


class TestTaskReasoningEffortConfig:
    def test_none_on_custom_profile_emits_ollama_wire(self):
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": "none"},
        ):
            kwargs = _build_call_kwargs(
                "custom", "gemma4:26b-mlx", _msgs(), task="compression",
            )
        # Profile-owned shape: top-level none + Ollama think flag.
        assert kwargs.get("reasoning_effort") == "none"
        assert kwargs["extra_body"]["think"] is False

    def test_false_bool_means_disabled(self):
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": False},
        ):
            kwargs = _build_call_kwargs(
                "custom", "gemma4:26b-mlx", _msgs(), task="compression",
            )
        assert kwargs.get("reasoning_effort") == "none"
        assert kwargs["extra_body"]["think"] is False

    def test_valid_level_maps_to_top_level_effort(self):
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": "low"},
        ):
            kwargs = _build_call_kwargs(
                "custom", "gemma4:26b-mlx", _msgs(), task="compression",
            )
        # Enabled-effort: profile emits the level, no think flag.
        assert kwargs.get("reasoning_effort") == "low"
        assert "think" not in kwargs.get("extra_body", {})

    def test_explicit_reasoning_config_wins(self):
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": "high"},
        ):
            kwargs = _build_call_kwargs(
                "custom", "gemma4:26b-mlx", _msgs(), task="compression",
                reasoning_config={"enabled": False},
            )
        # Explicit per-call config is not overridden by task config.
        assert kwargs.get("reasoning_effort") == "none"
        assert kwargs["extra_body"]["think"] is False

    def test_no_task_config_no_reasoning_fields(self):
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={},
        ):
            kwargs = _build_call_kwargs(
                "custom", "gemma4:26b-mlx", _msgs(), task="compression",
            )
        assert "reasoning_effort" not in kwargs
        assert "think" not in (kwargs.get("extra_body") or {})

    def test_unrecognized_value_warns_and_is_ignored(self, caplog):
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": "banana"},
        ):
            kwargs = _build_call_kwargs(
                "custom", "gemma4:26b-mlx", _msgs(), task="compression",
            )
        assert "reasoning_effort" not in kwargs
        assert "think" not in (kwargs.get("extra_body") or {})
        assert "not recognized" in caplog.text

    def test_no_task_no_knob_read(self):
        # No task → knob not even consulted; behavior identical to pre-patch.
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": "none"},
        ) as cfg:
            kwargs = _build_call_kwargs(
                "custom", "gemma4:26b-mlx", _msgs(),
            )
        assert "reasoning_effort" not in kwargs
        assert "think" not in (kwargs.get("extra_body") or {})
        cfg.assert_not_called()

    def test_fallback_top_level_key_for_profileless_provider(self):
        # A provider with NO reasoning-aware profile must still get the
        # top-level key when the operator set the task knob — this is the
        # generic path that covers arbitrary OpenAI-compatible backends.
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": "none"},
        ):
            kwargs = _build_call_kwargs(
                "not-a-real-provider-xyz", "some-model", _msgs(),
                task="compression",
            )
        assert kwargs.get("reasoning_effort") == "none"
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_fallback_not_fired_without_task_config(self):
        # Internal per-call reasoning_config alone must NOT produce a
        # top-level key on a profileless provider (strict-gateway safety).
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={},
        ):
            kwargs = _build_call_kwargs(
                "not-a-real-provider-xyz", "some-model", _msgs(),
                task="compression",
                reasoning_config={"enabled": False},
            )
        assert "reasoning_effort" not in kwargs
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_explicit_extra_body_reasoning_survives_profile(self):
        # Review F1: a caller-supplied extra_body.reasoning must survive
        # when the profile handles reasoning in its OWN dialect (top-level
        # key) and never injected the generic object itself — the pop is
        # gated on the profile being the source.
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": "none"},
        ):
            kwargs = _build_call_kwargs(
                "custom", "gemma4:26b-mlx", _msgs(), task="compression",
                extra_body={"reasoning": {"enabled": False}},
            )
        assert kwargs["extra_body"]["reasoning"] == {"enabled": False}

    def test_pinned_level_emitted_on_profile_less_backend(self):
        # Review F2: a config-pinned LEVEL (not just "none") reaches the
        # wire as a top-level reasoning_effort on a backend with no
        # reasoning-aware profile.
        with patch(
            "agent.auxiliary_client._get_auxiliary_task_config",
            return_value={"reasoning_effort": "low"},
        ):
            kwargs = _build_call_kwargs(
                "not-a-real-provider-xyz", "some-model", _msgs(),
                task="compression",
            )
        assert kwargs.get("reasoning_effort") == "low"
        assert kwargs["extra_body"]["reasoning"] == {"enabled": True, "effort": "low"}
