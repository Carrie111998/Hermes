"""Tests for agent.redact.redact_object -- the recursive structure walker.

``redact_sensitive_text`` only handles ``str``. Request bodies, session logs
and transcript entries are nested containers, so the persistence sinks in
``run_agent.py`` and ``gateway/session.py`` need a walker. These tests pin the
behaviour that those sinks depend on.

Phase 9 / Packet B1.
"""

import pytest

from agent.redact import REDACTED_PLACEHOLDER, redact_object


class TestStringLeaves:
    def test_redacts_nested_string_leaf(self):
        payload = {"messages": [{"role": "user", "content": "my key is sk-proj-abc123def456ghi789"}]}
        result = redact_object(payload)
        content = result["messages"][0]["content"]
        assert "abc123def456ghi789" not in content
        assert "sk-pro" in content

    def test_non_string_scalars_pass_through(self):
        payload = {"n": 42, "f": 1.5, "b": True, "none": None}
        assert redact_object(payload) == {"n": 42, "f": 1.5, "b": True, "none": None}

    def test_bytes_reduced_to_length_marker(self):
        result = redact_object({"blob": b"sk-proj-abc123def456ghi789"})
        assert result["blob"] == "[REDACTED BYTES len=26]"


class TestKeyBasedRedaction:
    """The value-based matchers only recognise known shapes. Key matching is
    what catches opaque credentials that match nothing."""

    def test_opaque_value_under_sensitive_key_is_redacted(self):
        # No vendor prefix, no JWT shape, no entropy heuristic would flag this.
        payload = {"api_key": "wibble-wobble-plain-english"}
        assert redact_object(payload)["api_key"] == REDACTED_PLACEHOLDER

    def test_authorization_key_redacted(self):
        payload = {"headers": {"Authorization": "Bearer totally-opaque-value"}}
        assert redact_object(payload)["headers"]["Authorization"] == REDACTED_PLACEHOLDER

    def test_hyphen_normalised_to_underscore(self):
        assert redact_object({"api-key": "opaque"})["api-key"] == REDACTED_PLACEHOLDER

    def test_case_insensitive(self):
        assert redact_object({"ApiKey": "opaque"})["ApiKey"] == REDACTED_PLACEHOLDER

    def test_none_value_stays_none(self):
        """Don't invent a placeholder where there was no value."""
        assert redact_object({"token": None})["token"] is None

    @pytest.mark.parametrize("key", ["token_count", "session_id", "keyboard", "authorization_url"])
    def test_exact_match_not_substring(self, key):
        """``token_count`` must not be caught by ``token``. Substring matching
        here would silently destroy ordinary telemetry."""
        assert redact_object({key: 123})[key] == 123


class TestContainers:
    def test_list_walked(self):
        result = redact_object(["sk-proj-abc123def456ghi789", "safe"])
        assert "abc123def456ghi789" not in result[0]
        assert result[1] == "safe"

    def test_tuple_stays_tuple(self):
        result = redact_object(("safe",))
        assert isinstance(result, tuple)

    def test_set_becomes_list(self):
        """Sets are not JSON-serialisable and every caller serialises to JSON."""
        result = redact_object({"s": {"safe"}})
        assert result["s"] == ["safe"]

    def test_deeply_nested(self):
        payload = {"a": [{"b": ({"c": [{"api_key": "opaque"}]},)}]}
        assert payload["a"][0]["b"][0]["c"][0]["api_key"] == "opaque"  # sanity
        result = redact_object(payload)
        assert result["a"][0]["b"][0]["c"][0]["api_key"] == REDACTED_PLACEHOLDER


class TestSafety:
    def test_input_not_mutated(self):
        """The sinks redact live in-memory conversation state on its way to
        disk. Mutating the original would corrupt the running agent."""
        payload = {"api_key": "opaque", "messages": [{"content": "sk-proj-abc123def456ghi789"}]}
        redact_object(payload)
        assert payload["api_key"] == "opaque"
        assert payload["messages"][0]["content"] == "sk-proj-abc123def456ghi789"

    def test_circular_reference_terminates(self):
        payload = {"name": "root"}
        payload["self"] = payload
        result = redact_object(payload)
        assert result["self"] == "[REDACTED: circular reference]"

    def test_circular_via_list_terminates(self):
        items = ["a"]
        items.append(items)
        result = redact_object({"items": items})
        assert result["items"][1] == "[REDACTED: circular reference]"

    def test_shared_child_is_not_a_cycle(self):
        """Cycle detection is path-based: the same dict appearing as two
        siblings is a DAG, not a cycle, and both must still be redacted."""
        shared = {"api_key": "opaque"}
        result = redact_object({"x": shared, "y": shared})
        assert result["x"]["api_key"] == REDACTED_PLACEHOLDER
        assert result["y"]["api_key"] == REDACTED_PLACEHOLDER

    def test_depth_cap(self):
        node = {"api_key": "opaque"}
        for _ in range(80):
            node = {"child": node}
        result = redact_object(node, max_depth=8)
        flat = repr(result)
        assert "max depth exceeded" in flat
        assert "opaque" not in flat

    def test_unknown_object_stringified_and_redacted(self):
        """json.dumps(..., default=str) stringifies unknown objects AFTER we
        redact. If the walker did not pre-stringify them, a secret in __repr__
        would reach the file unredacted."""

        class Opaque:
            def __repr__(self):
                return "Opaque(key=sk-proj-abc123def456ghi789)"

        result = redact_object({"obj": Opaque()})
        assert "abc123def456ghi789" not in result["obj"]


class TestForceDefault:
    def test_force_true_by_default_when_global_redaction_disabled(self, monkeypatch):
        """THE critical property for the persistence sinks.

        ``_REDACT_ENABLED`` is snapshotted at import from HERMES_REDACT_SECRETS.
        If the config bridge in hermes_cli/main.py has not run, or the operator
        turned logging redaction off, disk persistence must redact anyway.
        """
        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
        result = redact_object({"content": "sk-proj-abc123def456ghi789"})
        assert "abc123def456ghi789" not in result["content"]

    def test_key_based_redaction_also_survives_disabled_global(self, monkeypatch):
        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
        assert redact_object({"api_key": "opaque"})["api_key"] == REDACTED_PLACEHOLDER

    def test_force_false_honours_global_flag(self, monkeypatch):
        """force=False must still defer to the global flag -- callers that are
        not persistence boundaries keep the old opt-in behaviour."""
        monkeypatch.setattr("agent.redact._REDACT_ENABLED", False)
        result = redact_object({"content": "sk-proj-abc123def456ghi789"}, force=False)
        assert result["content"] == "sk-proj-abc123def456ghi789"


class TestAmbiguousKeyGuard:
    """Phase 9 / C2 -- regression for the `key` false positive.

    `browser_press` takes a parameter literally named `key`, holding "Enter",
    "Tab", "ArrowDown". Blanket key-based redaction destroyed both the recorded
    tool call and the tool schema's property definition in every session log
    and gateway transcript.
    """

    @pytest.mark.parametrize("value", ["Enter", "Tab", "Escape", "ArrowDown", "F5", "ctrl-c"])
    def test_short_identifier_shaped_values_kept(self, value):
        assert redact_object({"key": value})["key"] == value

    def test_browser_press_tool_call_intact(self):
        call = {"name": "browser_press", "arguments": {"key": "Enter"}}
        assert redact_object(call)["arguments"]["key"] == "Enter"

    def test_browser_press_tool_schema_intact(self):
        """The dict-valued case. A string-only guard would still have erased
        this -- the schema's value is a dict, not a string."""
        schema = {
            "name": "browser_press",
            "parameters": {
                "properties": {
                    "key": {"type": "string", "description": "Key to press (e.g., 'Enter')"}
                },
                "required": ["key"],
            },
        }
        prop = redact_object(schema)["parameters"]["properties"]["key"]
        assert isinstance(prop, dict), "schema property was replaced wholesale"
        assert prop["type"] == "string"
        assert "Enter" in prop["description"]

    def test_list_under_ambiguous_key_is_recursed(self):
        out = redact_object({"key": ["Enter", "Tab"]})
        assert out["key"] == ["Enter", "Tab"]

    def test_non_string_scalar_under_ambiguous_key_kept(self):
        assert redact_object({"key": 27})["key"] == 27

    # --- containment must NOT be weakened ------------------------------------

    @pytest.mark.parametrize("value", [
        "Zq7Z4mKp2Wf9Lx3Rv8Tn1Yb6Hd5Gs0Jc",          # opaque, 32 chars
        "sk-proj-CANARYaaaabbbbccccddddeeeeffff",     # vendor-prefixed
        "this value has spaces and is long",          # not identifier-shaped
        "0123456789abcdef0123",                       # starts with a digit
    ])
    def test_credential_shaped_values_still_redacted_under_bare_key(self, value):
        assert redact_object({"key": value})["key"] == REDACTED_PLACEHOLDER

    @pytest.mark.parametrize("keyname", [
        "api_key", "apikey", "token", "secret", "password",
        "authorization", "access_token", "private_key", "client_secret",
    ])
    def test_unambiguous_keys_have_no_shape_exemption(self, keyname):
        """The guard is scoped to bare `key` only. A short value under any
        unambiguous name is still redacted wholesale."""
        assert redact_object({keyname: "Enter"})[keyname] == REDACTED_PLACEHOLDER

    def test_unambiguous_key_with_dict_value_still_wholesale(self):
        """Only the ambiguous name recurses. `{"api_key": {...}}` is odd enough
        that replacing it wholesale remains the safe reading."""
        out = redact_object({"api_key": {"nested": "anything"}})
        assert out["api_key"] == REDACTED_PLACEHOLDER


class TestKnownLimitation:
    """Documented boundary, asserted so a future change is visible."""

    def test_short_credential_under_bare_key_survives(self):
        """RESIDUAL introduced by the C2 guard -- the price of keeping
        `browser_press` readable, asserted rather than left to be discovered.

        A short, identifier-shaped credential under a bare `key` is now kept.
        Unambiguous key names have no such exemption, and any credential long
        enough to be high-entropy still fails the shape test.
        """
        assert redact_object({"key": "hunter2"})["key"] == "hunter2"

    def test_high_entropy_value_under_innocuous_key_is_NOT_caught(self):
        """A credential matching no vendor prefix, stored under a key that is
        not in _SENSITIVE_BODY_KEYS, survives value-based matching.

        This is why the canary suite (B5) includes high-entropy canaries in
        no known format: it measures this gap rather than pretending it is
        closed. Change this test only alongside a real matcher improvement.
        """
        canary = "Zq7Z4mKp2Wf9Lx3Rv8Tn1Yb6Hd5Gs0Jc"
        result = redact_object({"note": canary})
        assert result["note"] == canary
