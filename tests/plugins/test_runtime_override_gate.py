"""Trust gate for pre_llm_call runtime_override (#23739).

A plugin may only return {"runtime_override": ...} from a pre_llm_call hook
when the operator opted in via plugins.entries.<id>.llm.allow_runtime_override
(bundled plugins are trusted).  Without the gate, enabling any plugin would
silently grant it the ability to redirect the whole session (base_url +
api_key override) to an arbitrary endpoint.
"""

from unittest.mock import patch

import pytest

from hermes_cli.plugins import PluginContext


class _FakeManifest:
    def __init__(self, name="my-plugin", key="my-plugin", source="local"):
        self.name = name
        self.key = key
        self.source = source


class _FakeManager:
    home_path = "/tmp/nonexistent-plugin-home"


def _ctx(source="local", name="my-plugin", key="my-plugin") -> PluginContext:
    ctx = object.__new__(PluginContext)
    ctx.manifest = _FakeManifest(name=name, key=key, source=source)
    ctx._manager = _FakeManager()
    return ctx


class TestGateRuntimeOverride:
    def test_denied_plugin_strips_runtime_override(self):
        ctx = _ctx(source="local")

        def cb(**kw):
            return {"context": "keep me", "runtime_override": {"model": "gpt-5.6"}}

        gated = ctx._gate_runtime_override(cb)
        assert gated() == {"context": "keep me"}

    def test_strip_preserves_non_dict_and_string_results(self):
        ctx = _ctx(source="local")

        def cb_plain(**kw):
            return "plain string context"

        gated = ctx._gate_runtime_override(cb_plain)
        assert gated() == "plain string context"

    def test_wrapper_preserves_signature_for_payload_filtering(self):
        ctx = _ctx(source="local")

        def cb(session_id, user_message):
            return {"runtime_override": {"model": "x"}}

        gated = ctx._gate_runtime_override(cb)
        # functools.wraps must keep the narrow signature so the additive-payload
        # filter in _invoke_hook_callback still sees the original contract.
        import inspect

        params = list(inspect.signature(gated).parameters)
        assert "session_id" in params
        assert "user_message" in params
        # Narrow call still works through the wrapper.
        assert gated(session_id="s", user_message="u") == {}


class TestRuntimeOverrideAllowed:
    def test_bundled_is_trusted(self):
        assert _ctx(source="bundled")._runtime_override_allowed() is True

    def test_local_denied_by_default(self):
        with patch(
            "hermes_cli.config.load_config", return_value={}
        ) as _cfg:
            assert _ctx(source="local")._runtime_override_allowed() is False

    def test_local_allowed_with_config_opt_in(self):
        cfg = {
            "plugins": {
                "entries": {
                    "my-plugin": {"llm": {"allow_runtime_override": True}}
                }
            }
        }
        with patch("hermes_cli.config.load_config", return_value=cfg) as _cfg:
            assert _ctx(source="local")._runtime_override_allowed() is True

    def test_config_read_failure_fails_closed(self):
        with patch(
            "hermes_cli.config.load_config",
            side_effect=RuntimeError("boom"),
        ) as _cfg:
            assert _ctx(source="local")._runtime_override_allowed() is False
