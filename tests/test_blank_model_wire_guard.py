"""Regression tests for the blank-model wire guard.

A mid-session provider switch could leave request-construction state
stale: the turn context resolved the correct model, but the api_kwargs
reaching the wire carried an empty model string. Relays reject that with
a misleading auth-shaped error — OpenCode Go returns
``HTTP 401 Model  is not supported`` (empty model interpolated).
Reproduced byte-for-byte against the live Go relay 2026-08-23.
"""

from agent.chat_completion_helpers import _repair_blank_model


class _Agent:
    def __init__(self, model="", provider=""):
        self.model = model
        self.provider = provider


def test_blank_model_repaired_from_agent():
    kwargs = {"model": "", "messages": []}
    _repair_blank_model(_Agent("ox-alpha-free", "opencode-go"), kwargs)
    assert kwargs["model"] == "ox-alpha-free"


def test_provider_prefixed_model_normalized():
    kwargs = {"model": None}
    _repair_blank_model(_Agent("opencode-go/ox-alpha-free", "opencode-go"), kwargs)
    assert kwargs["model"] == "ox-alpha-free"


def test_nonblank_model_never_rewritten():
    """The guard only fills blanks — never second-guesses a set model."""
    kwargs = {"model": "glm-5.2"}
    _repair_blank_model(_Agent("ox-alpha-free", "opencode-go"), kwargs)
    assert kwargs["model"] == "glm-5.2"


def test_both_blank_stays_blank_without_crash():
    """Agent with no model either: honest failure downstream, no crash here."""
    kwargs = {"model": ""}
    _repair_blank_model(_Agent("", "opencode-go"), kwargs)
    assert kwargs["model"] == ""


def test_agent_missing_model_attr_no_crash():
    class Bare:
        pass

    kwargs = {"model": ""}
    _repair_blank_model(Bare(), kwargs)  # must not raise
    assert kwargs["model"] == ""


def test_plain_provider_repair_uses_live_value():
    kwargs = {"model": ""}
    _repair_blank_model(_Agent("deepseek-v4-flash", "deepseek"), kwargs)
    assert kwargs["model"] == "deepseek-v4-flash"
