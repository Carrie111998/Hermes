"""_summarize_cron_failure_for_delivery must classify on the error's first
line (the exception line), never on substrings embedded in later payload
lines.

Field-reported (#99988): a monitor-job creation bug stored the whole script
SOURCE in ``monitor_script`` (which must hold a filename), so every run died
with ``[Errno 63] File name too long: '/home/user/.hermes/scripts/#!/bin/bash
... Authorization: Bearer $TOKEN ...'`` — OSError embeds the whole too-long
"filename", newlines included, and the embedded curl header's word
"Authorization" tripped the auth substring match. The operator was delivered
"provider authentication error" for a crash that never opened a provider
socket and was told to debug the wrong subsystem. Replacing the single word
"Authorization" with "Xuthorization" flipped the label — the classifier was
reading the crashed script's own source code as an error signature.

The no-agent script path already guards this class via its mode gate
(test_no_agent_failure_never_blamed_on_a_provider); an agent-mode job's error
text can embed the same attacker-content-shaped strings (OSError filenames,
captured subprocess output, tool payloads in tracebacks), so the provider
branches match the FIRST line only: provider errors arrive as single-line
``str(exc)`` ("Error code: 401 - ...", "httpx.ReadTimeout: ..."), while
anything on later lines is payload, not signature.
"""

from cron.scheduler import _summarize_cron_failure_for_delivery


def _agent_job():
    return {"name": "watch-manifest", "id": "m111", "no_agent": False}


def test_file_name_too_long_embedding_auth_wording_not_provider_auth():
    """#99988 replay: an OSError whose embedded filename is a whole script
    whose source contains "Authorization" must not be labeled a provider
    authentication error."""
    error = (
        "[Errno 63] File name too long: '/home/user/.hermes/scripts/#!/bin/bash\n"
        "# watch the deployment manifest\n"
        'TOKEN=$(curl -s "https://auth.example/token")\n'
        'MANIFEST=$(curl -s -H "Authorization: Bearer $TOKEN" '
        "https://registry.example/manifests/latest)"
    )
    msg = _summarize_cron_failure_for_delivery(_agent_job(), error)
    assert "provider authentication error" not in msg
    assert "provider" not in msg.lower()
    # The generic cleaner names what actually failed.
    assert "File name too long" in msg


def test_embedded_timeout_wording_in_payload_not_provider_timeout():
    """A crash whose payload lines merely mention "timeout" must not be
    rewritten into "provider timeout / fallback chain" prose."""
    error = (
        "OSError: [Errno 5] Input/output error: '/var/lib/hermes/state.db'\n"
        "# retry loop: sleep 30 then retry on timeout after 30s"
    )
    msg = _summarize_cron_failure_for_delivery(_agent_job(), error)
    assert "provider timeout" not in msg
    assert "fallback chain" not in msg.lower()
    assert "Input/output error" in msg


def test_embedded_rate_limit_wording_in_payload_not_rate_limit():
    """A script's captured log line "HTTP 429" on a later line is payload,
    not the crash's signature."""
    error = (
        "RuntimeError: script produced invalid JSON for manifest\n"
        "upstream log: HTTP 429 Too Many Requests (backoff applied)"
    )
    msg = _summarize_cron_failure_for_delivery(_agent_job(), error)
    assert "provider rate limit" not in msg
    assert "provider" not in msg.lower()
    assert "invalid JSON" in msg


def test_first_line_signature_in_a_multiline_error_still_classifies():
    """Genuine provider errors can carry payload on later lines; the
    signature on the FIRST line must still classify."""
    error = (
        "Error code: 429 - {'error': {'message': 'rate limit exceeded'}}\n"
        "request-id: req_abc123\n"
        "retry-after: 60"
    )
    msg = _summarize_cron_failure_for_delivery(_agent_job(), error)
    assert "provider rate limit" in msg


def test_first_line_401_still_classifies_as_provider_auth():
    error = "Error code: 401 - {'error': {'message': 'Invalid API key'}}"
    msg = _summarize_cron_failure_for_delivery(_agent_job(), error)
    assert "provider authentication error" in msg
