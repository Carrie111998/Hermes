from types import SimpleNamespace

import pytest

from agent.conversation_loop import (
    _invalid_response_error_code,
    _invalid_response_failure_hint,
)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (None, None),
        (SimpleNamespace(error=None), None),
        (SimpleNamespace(error=SimpleNamespace(code="524")), 524),
        (SimpleNamespace(error={"code": 429}), 429),
        (SimpleNamespace(error=SimpleNamespace(code="not-an-int")), None),
        (SimpleNamespace(error={"message": "missing code"}), None),
    ],
)
def test_invalid_response_error_code(response, expected):
    assert _invalid_response_error_code(response) == expected


@pytest.mark.parametrize(
    ("error_code", "duration", "expected"),
    [
        (524, 12.6, "upstream provider timed out (Cloudflare 524, 13s)"),
        (504, 12.6, "upstream gateway timeout (504, 13s)"),
        (429, 1.0, "rate limited by upstream provider (429)"),
        (500, 12.6, "upstream server error (500, 13s)"),
        (502, 12.6, "upstream server error (502, 13s)"),
        (503, 12.6, "upstream provider overloaded (503)"),
        (529, 12.6, "upstream provider overloaded (529)"),
        (418, 12.6, "upstream error (code 418, 13s)"),
        (None, 3.25, "fast response (3.2s) — likely rate limited"),
        (None, 61.2, "slow response (61s) — likely upstream timeout"),
        (None, 10.0, "response time 10.0s"),
        (None, 60.0, "response time 60.0s"),
    ],
)
def test_invalid_response_failure_hint(error_code, duration, expected):
    assert _invalid_response_failure_hint(error_code, duration) == expected
