"""Regression tests for credential capture in _dump_api_request_debug.

Phase 9 / Packet B2.

The defect these pin: an agent that reads a credential into its context carries
that credential in the conversation payload, so `body`, the provider error
body, and the provider response text are all credential-bearing. They were
serialized verbatim to `request_dump_*.json` (world-readable 644) and, when
HERMES_DUMP_REQUEST_STDOUT was set, printed to stdout into gateway.log.

Each test here fails if its seam is removed.
"""

import json
import os
import stat

from tests.run_agent.test_run_agent_codex_responses import _patch_agent_bootstrap

import run_agent


# A credential shape the value-based matchers DO recognise.
CANARY_PREFIXED = "sk-proj-CANARYaaaabbbbccccddddeeeeffff"
# A credential shape they do NOT recognise -- only key-based matching catches
# this one. Deliberate: it proves the sink does not rely on vendor patterns.
CANARY_OPAQUE = "Zq7Z4mKp2Wf9Lx3Rv8Tn1Yb6Hd5Gs0Jc"


def _agent(monkeypatch, tmp_path):
    _patch_agent_bootstrap(monkeypatch)
    agent = run_agent.AIAgent(
        model="gpt-4o",
        base_url="http://127.0.0.1:9208/v1",
        api_key="test-key",
        quiet_mode=True,
        max_iterations=1,
        skip_context_files=True,
        skip_memory=True,
    )
    agent.logs_dir = tmp_path
    return agent


def _kwargs_with_secret_in_conversation():
    """The real capture path: the credential arrives as conversation text."""
    return {
        "model": "gpt-4o",
        "messages": [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": f"Here is my .env: YELP_API_KEY={CANARY_PREFIXED}"},
            {"role": "assistant", "content": f"Noted, the opaque one is {CANARY_OPAQUE}"},
        ],
        "temperature": 0.7,
    }


class TestFileSink:
    def test_conversation_content_not_persisted_by_default(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)
        dump_file = agent._dump_api_request_debug(
            _kwargs_with_secret_in_conversation(), reason="preflight"
        )
        raw = dump_file.read_text()
        assert CANARY_PREFIXED not in raw
        assert CANARY_OPAQUE not in raw

    def test_debugging_shape_is_retained(self, monkeypatch, tmp_path):
        """Containment must not make the dump useless -- roles, sizes, model
        and sampling params are what a 4xx diagnosis actually needs."""
        agent = _agent(monkeypatch, tmp_path)
        dump_file = agent._dump_api_request_debug(
            _kwargs_with_secret_in_conversation(), reason="preflight"
        )
        payload = json.loads(dump_file.read_text())
        body = payload["request"]["body"]

        assert body["model"] == "gpt-4o"
        assert body["temperature"] == 0.7
        assert [m["role"] for m in body["messages"]] == ["system", "user", "assistant"]
        assert body["messages"][1]["content"]["type"] == "str"
        assert body["messages"][1]["content"]["length"] > 0
        assert payload["body_mode"] == "projected (metadata only)"

    def test_dump_file_is_not_world_readable(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)
        dump_file = agent._dump_api_request_debug(
            {"model": "gpt-4o", "messages": []}, reason="preflight"
        )
        mode = stat.S_IMODE(os.stat(dump_file).st_mode)
        assert mode == 0o600, f"expected 0600, got {oct(mode)}"

    def test_full_body_opt_in_is_still_redacted(self, monkeypatch, tmp_path):
        """The escape hatch restores content for debugging but must not
        restore plaintext credentials. There is no config that writes an
        unredacted body to disk."""
        monkeypatch.setenv("HERMES_DUMP_REQUEST_FULL_BODY", "1")
        agent = _agent(monkeypatch, tmp_path)
        dump_file = agent._dump_api_request_debug(
            _kwargs_with_secret_in_conversation(), reason="preflight"
        )
        raw = dump_file.read_text()
        payload = json.loads(raw)

        # Content is present again...
        assert "helpful assistant" in raw
        assert payload["body_mode"].startswith("full")
        # ...but the recognisable credential is not.
        assert CANARY_PREFIXED not in raw

    def test_full_body_opt_in_does_NOT_contain_opaque_credentials(
        self, monkeypatch, tmp_path
    ):
        """Measured limitation of the escape hatch -- asserted, not assumed.

        Under HERMES_DUMP_REQUEST_FULL_BODY the conversation text is written
        back. Value-based matching only recognises known shapes, and a
        high-entropy credential in no vendor format, sitting under an
        innocuous key like "content", matches nothing. It survives.

        This is why the flag is debugging-only and must not be left enabled:
        the default projected path contains it (by dropping content entirely),
        the full-body path does not. If a future matcher closes this gap, this
        test will fail and should be updated deliberately.
        """
        monkeypatch.setenv("HERMES_DUMP_REQUEST_FULL_BODY", "1")
        agent = _agent(monkeypatch, tmp_path)
        dump_file = agent._dump_api_request_debug(
            _kwargs_with_secret_in_conversation(), reason="preflight"
        )
        assert CANARY_OPAQUE in dump_file.read_text()

    def test_default_projected_path_DOES_contain_opaque_credentials(
        self, monkeypatch, tmp_path
    ):
        """The counterpart: the default path contains what redaction cannot,
        because it drops conversation content rather than trying to match it.
        This is the reason projection is the default and not merely a nicety."""
        agent = _agent(monkeypatch, tmp_path)
        dump_file = agent._dump_api_request_debug(
            _kwargs_with_secret_in_conversation(), reason="preflight"
        )
        assert CANARY_OPAQUE not in dump_file.read_text()


class TestErrorSinks:
    """The two sinks that carry PROVIDER-side text: error.body (:4286 pre-fix)
    and error.response_text (:4292 pre-fix)."""

    def test_error_body_redacted(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)

        error = RuntimeError("bad request")
        error.body = {"message": f"invalid key {CANARY_PREFIXED}", "api_key": CANARY_OPAQUE}

        dump_file = agent._dump_api_request_debug(
            {"model": "gpt-4o", "messages": []}, reason="error", error=error
        )
        raw = dump_file.read_text()
        assert CANARY_PREFIXED not in raw
        assert CANARY_OPAQUE not in raw

    def test_error_response_text_redacted(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)

        class _Response:
            status_code = 400
            text = f'{{"error": "rejected", "token": "{CANARY_PREFIXED}"}}'

        error = RuntimeError("bad request")
        error.response = _Response()

        dump_file = agent._dump_api_request_debug(
            {"model": "gpt-4o", "messages": []}, reason="error", error=error
        )
        assert CANARY_PREFIXED not in dump_file.read_text()

    def test_error_message_redacted(self, monkeypatch, tmp_path):
        agent = _agent(monkeypatch, tmp_path)
        error = RuntimeError(f"auth failed for {CANARY_PREFIXED}")
        dump_file = agent._dump_api_request_debug(
            {"model": "gpt-4o", "messages": []}, reason="error", error=error
        )
        assert CANARY_PREFIXED not in dump_file.read_text()


class TestStdoutSink:
    """HERMES_DUMP_REQUEST_STDOUT feeds gateway.log. A fix applied only at the
    file write would leave this path open -- that is the whole reason the
    redaction seam sits above serialization rather than at the write."""

    def test_stdout_path_redacted(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setenv("HERMES_DUMP_REQUEST_STDOUT", "1")
        agent = _agent(monkeypatch, tmp_path)
        agent._dump_api_request_debug(
            _kwargs_with_secret_in_conversation(), reason="preflight"
        )
        printed = capsys.readouterr().out
        assert printed.strip(), "expected the stdout dump to actually be printed"
        assert CANARY_PREFIXED not in printed
        assert CANARY_OPAQUE not in printed

    def test_stdout_and_file_carry_the_same_payload(self, monkeypatch, tmp_path, capsys):
        """Both consume the same redacted serialization -- so they cannot
        drift apart and leave one path unprotected.

        Containment, not equality: stdout also carries the _vprint notice and
        any provider warnings, so the file content must be a substring of it.
        """
        monkeypatch.setenv("HERMES_DUMP_REQUEST_STDOUT", "1")
        agent = _agent(monkeypatch, tmp_path)
        dump_file = agent._dump_api_request_debug(
            _kwargs_with_secret_in_conversation(), reason="preflight"
        )
        printed = capsys.readouterr().out
        assert dump_file.read_text().strip() in printed


class TestAuthHeaderStillMasked:
    def test_authorization_header_not_plaintext(self, monkeypatch, tmp_path):
        """Pre-existing behaviour (_mask_api_key_for_logs) must survive the
        change -- redact_object now also key-matches 'Authorization'."""
        agent = _agent(monkeypatch, tmp_path)
        dump_file = agent._dump_api_request_debug(
            {"model": "gpt-4o", "messages": []}, reason="preflight"
        )
        payload = json.loads(dump_file.read_text())
        assert "test-key" not in json.dumps(payload)
