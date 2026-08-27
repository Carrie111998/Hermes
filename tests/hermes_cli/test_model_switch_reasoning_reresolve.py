"""Regression tests for the /model reasoning re-resolution (#96012).

``self.reasoning_config`` was resolved once at startup against the default
model. A mid-session ``/model`` switch updated the agent in place with the
correct per-model ``reasoning_overrides`` value, but the CLI kept the stale
startup-resolved config — and the next agent re-initialization injected it
back, clobbering the override. Providers that accept a narrower level set
(e.g. GLM-5.3-Flash: low/high/max only) then 400 on the stale level.

The switch path now re-runs the shared chokepoint
(``resolve_reasoning_config``) for the NEW model, an explicit ``--reasoning``
stays authoritative for the whole run, and a failed in-place agent swap rolls
the reasoning config back with the rest of the CLI snapshot.
"""

from __future__ import annotations

from hermes_cli.model_switch import ModelSwitchResult


class _FakeModelInfo:
    context_window = 128_000
    max_output = 0

    def has_cost_data(self):
        return False

    def format_capabilities(self):
        return ""


class _StubCLI:
    """Minimum attrs ``_apply_model_switch_result`` reads on ``self``."""

    agent = None
    model = "old-default-model"
    provider = ""
    requested_provider = ""
    api_key = ""
    _explicit_api_key = ""
    base_url = ""
    _explicit_base_url = ""
    api_mode = ""
    _pending_model_switch_note = ""
    reasoning_config = {"effort": "medium"}
    _reasoning_cli_flag_applied = False


class _FailingAgent:
    def switch_model(self, **kwargs):
        raise RuntimeError("swap failed")


def _result(new_model="glm-5.3-flash"):
    return ModelSwitchResult(
        success=True,
        new_model=new_model,
        target_provider="zhipu",
        provider_changed=True,
        api_key="",
        base_url="",
        api_mode="",
        warning_message="",
        provider_label="",
        resolved_via_alias=False,
        capabilities=None,
        model_info=_FakeModelInfo(),
        is_global=False,
    )


def _apply(monkeypatch, stub):
    import cli as cli_mod

    monkeypatch.setattr(cli_mod, "_cprint", lambda s, *a, **k: None)
    monkeypatch.setattr(cli_mod, "save_config_value", lambda *a, **k: None)
    cli_mod.HermesCLI._apply_model_switch_result(stub, _result(), False)


def test_switch_reresolves_reasoning_for_new_model(monkeypatch):
    resolved_for: list[str] = []

    def _fake_resolve(cfg, model=""):
        resolved_for.append(model)
        return {"effort": "high"}  # the per-model override for glm-5.3-flash

    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config", _fake_resolve
    )
    stub = _StubCLI()
    _apply(monkeypatch, stub)

    assert resolved_for == ["glm-5.3-flash"]
    assert stub.reasoning_config == {"effort": "high"}


def test_explicit_reasoning_flag_survives_switch(monkeypatch):
    def _fake_resolve(cfg, model=""):
        raise AssertionError("must not re-resolve: --reasoning is authoritative")

    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config", _fake_resolve
    )
    stub = _StubCLI()
    stub._reasoning_cli_flag_applied = True
    stub.reasoning_config = {"effort": "low"}  # from --reasoning

    _apply(monkeypatch, stub)

    assert stub.reasoning_config == {"effort": "low"}


def test_failed_agent_swap_rolls_back_reasoning_config(monkeypatch):
    monkeypatch.setattr(
        "hermes_constants.resolve_reasoning_config",
        lambda cfg, model="": {"effort": "high"},
    )
    stub = _StubCLI()
    stub.agent = _FailingAgent()

    _apply(monkeypatch, stub)

    # The swap failed → the CLI snapshot (reasoning included) is restored.
    assert stub.reasoning_config == {"effort": "medium"}
    assert stub.model == "old-default-model"
