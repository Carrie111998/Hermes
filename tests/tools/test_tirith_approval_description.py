"""Tirith approval descriptions compose from the structured remediation field (#93839).

tirith (upstream, since v0.2.5) appends third-party promotional hints to the
free-text ``description`` of findings — e.g. the getvet.sh cross-promotion on
the "safer alternative" line — and ``_format_tirith_description`` rendered
that text verbatim into terminal approval prompts and platform approval
cards (Slack), where it reads as the agent recommending an unrelated
product. The clean structured guidance lives in ``remediation``; the
formatter now prefers it, falling back to ``description`` only for findings
that carry no remediation (no hardcoded promo-string filters, which upstream
wording changes would silently defeat).
"""

from tools.approval import _format_tirith_description


def test_promo_in_description_is_not_rendered_when_remediation_exists():
    """The reporter's exact finding shape: description carries the
    getvet.sh promo, remediation is the clean structured guidance."""
    result = {
        "action": "warn",
        "summary": "1 warning",
        "findings": [
            {
                "rule_id": "curl_pipe_shell",
                "severity": "HIGH",
                "title": "Pipe to interpreter: curl | python3",
                "description": (
                    "Command pipes output from 'curl' directly to interpreter "
                    "'python3'. Downloaded content will be executed without "
                    "inspection.\n  Safer: tirith run https://api.example.com/data "
                    "— or: vet https://api.example.com/data  (https://getvet.sh)"
                ),
                "remediation": (
                    "Download first with curl -o, review the script, then "
                    "execute. Or use tirith run <url>."
                ),
            }
        ],
    }

    out = _format_tirith_description(result)

    assert "getvet.sh" not in out
    assert "vet https://" not in out
    assert "Download first with curl -o" in out
    assert "Pipe to interpreter" in out
    assert "[HIGH]" in out


def test_description_fallback_when_no_remediation():
    """Findings without a remediation field keep the historical
    description-based rendering."""
    result = {
        "action": "warn",
        "summary": "1 warning",
        "findings": [
            {
                "severity": "LOW",
                "title": "Plain finding",
                "description": "Some plain detail text",
            }
        ],
    }

    out = _format_tirith_description(result)

    assert "Plain finding: Some plain detail text" in out


def test_mixed_findings_use_remediation_where_available():
    result = {
        "action": "block",
        "summary": "2 findings",
        "findings": [
            {
                "severity": "HIGH",
                "title": "With remediation",
                "description": "promo-laden description",
                "remediation": "clean guidance A",
            },
            {
                "severity": "LOW",
                "title": "Without remediation",
                "description": "plain description B",
            },
        ],
    }

    out = _format_tirith_description(result)

    assert "clean guidance A" in out
    assert "promo-laden description" not in out
    assert "plain description B" in out


def test_empty_string_remediation_falls_back_to_description():
    """``remediation: ""`` is falsy — the finding must keep the
    description-based rendering, not render an empty detail."""
    result = {
        "action": "warn",
        "summary": "1 warning",
        "findings": [
            {
                "severity": "MEDIUM",
                "title": "Empty remediation",
                "description": "plain fallback text",
                "remediation": "",
            }
        ],
    }

    out = _format_tirith_description(result)

    assert "Empty remediation: plain fallback text" in out


def test_non_string_remediation_falls_back_to_description():
    """A structured (list) remediation from a future tirith version must
    not render a Python repr into approval prompts."""
    result = {
        "action": "warn",
        "summary": "1 warning",
        "findings": [
            {
                "severity": "MEDIUM",
                "title": "Structured remediation",
                "description": "plain fallback text",
                "remediation": ["step one", "step two"],
            }
        ],
    }

    out = _format_tirith_description(result)

    assert "Structured remediation: plain fallback text" in out
    assert "step one" not in out
    assert "[" not in out.split("Structured remediation", 1)[-1]
