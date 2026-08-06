"""Startup context resolution for explicit model overrides."""

from agent import agent_init


def test_model_override_uses_matching_per_model_context_metadata():
    """`--model` must use its own metadata instead of the default model's pin."""
    resolver = getattr(agent_init, "_resolve_startup_model_context_length", None)
    assert resolver is not None, "startup per-model context resolver is missing"

    resolved = resolver(
        model_cfg={
            "default": "gpu-swe-qwen3",
            "provider": "custom",
            "base_url": "http://192.168.0.8:4000/v1",
            "context_length": 65_536,
            "models": {
                "code-gemma": {"context_length": 65_536},
            },
        },
        active_model="code-gemma",
        active_provider="custom",
        active_base_url="http://192.168.0.8:4000/v1",
    )

    assert resolved == 65_536


def test_model_override_does_not_inherit_unscoped_default_context_pin():
    """A different model without metadata must not inherit the default's window."""
    resolver = getattr(agent_init, "_resolve_startup_model_context_length", None)
    assert resolver is not None, "startup per-model context resolver is missing"

    resolved = resolver(
        model_cfg={
            "default": "gpu-swe-qwen3",
            "provider": "custom",
            "base_url": "http://192.168.0.8:4000/v1",
            "context_length": 65_536,
        },
        active_model="unknown-model",
        active_provider="custom",
        active_base_url="http://192.168.0.8:4000/v1",
    )

    assert resolved is None
