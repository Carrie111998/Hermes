"""Required canary suite: an ordinary agent read cannot return plaintext
from a credential-bearing file (Phase 9 / Packet C).

Synthetic values only. Every canary is exercised in BOTH directions --
the unsafe precondition on the underlying mechanism, and the corrected
behaviour through the real tool path -- so a green run cannot be mistaken
for a test that never had teeth.

Enforcement must not depend on the user's redaction/logging preference, so
the corrected-behaviour tests run with HERMES_REDACT_SECRETS explicitly unset.
"""

import importlib
import json
import os

import pytest

from agent import credential_tripwire as ct

# --- the required canaries ---------------------------------------------------

YELP_KEY = "YELP_API_KEY"
YELP_VALUE = "CANARYyelp_Bx7Qm2Zt9Kd4Rp1Wn6Fj3Hs8Lv5Gc0Ty-_Ee4Uu8Ii2Oo6Aa1"

OPQ_KEY = "OPAQUE_INTERNAL_SECRET"
OPQ_VALUE = "Zq7Z4mKp2Wf9Lx3Rv8Tn1Yb6Hd5Gs0Jc"

# A third canary the brief did not ask for, added because without it the suite
# would overclaim. _ENV_ASSIGN_RE keys off the VARIABLE NAME, so both canaries
# above are caught by name (API_KEY / SECRET) regardless of their value. This
# one has an unremarkable key name AND an opaque value, so no content pattern
# anywhere in agent/redact.py can match it in any presentation. Only
# path-based denial protects it.
PARTNER_KEY = "PARTNER_HANDSHAKE_VALUE"
PARTNER_VALUE = "Rk9Wc2Bn5Ht3Jq8Xm4Ld7Pz1Vy6Nf2E"

ENV_BODY = (
    f"# synthetic canary fixture\n"
    f"{YELP_KEY}={YELP_VALUE}\n"
    f"{OPQ_KEY}={OPQ_VALUE}\n"
    f"{PARTNER_KEY}={PARTNER_VALUE}\n"
)

ALL_CANARIES = [
    pytest.param(YELP_VALUE, id="yelp-known-shape"),
    pytest.param(OPQ_VALUE, id="opaque-secret-named"),
    pytest.param(PARTNER_VALUE, id="opaque-unremarkable-name"),
]


@pytest.fixture(autouse=True)
def _no_redaction_preference(monkeypatch):
    """Security must hold with the redaction preference OFF."""
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    yield


@pytest.fixture
def env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text(ENV_BODY)
    ct.reset_cache()
    yield f
    ct.reset_cache()


def assert_absent(blob: str, value: str, surface: str):
    assert value not in blob, f"CANARY LEAKED via {surface}: {value[:14]}..."


# =============================================================================
# Direction 1 — the unsafe precondition, on today's mechanism
# =============================================================================

def test_precondition_redaction_is_off_by_default():
    import agent.redact as redact
    importlib.reload(redact)
    assert redact._REDACT_ENABLED is False
    assert redact.redact_sensitive_text(ENV_BODY) == ENV_BODY


@pytest.mark.parametrize("value", ALL_CANARIES)
def test_precondition_code_file_true_returns_every_canary_verbatim(value):
    """The reported bypass: code_file=True disables dotenv detection, which is
    exactly what tools/file_tools.py passed on the read path."""
    from agent.redact import redact_sensitive_text
    out = redact_sensitive_text(ENV_BODY, force=True, code_file=True)
    assert value in out, "precondition: the bypass must be real"


@pytest.mark.parametrize("value", [OPQ_VALUE, PARTNER_VALUE, YELP_VALUE])
@pytest.mark.parametrize("force", [True, False])
@pytest.mark.parametrize("code_file", [True, False])
def test_precondition_no_content_pattern_catches_a_BARE_value(value, force, code_file):
    """The load-bearing precondition.

    Detached from its key name, none of these values matches any pattern in
    agent/redact.py under ANY configuration. No amount of redaction tuning
    could have fixed this; only path-based denial can.
    """
    from agent.redact import redact_sensitive_text
    assert redact_sensitive_text(value, force=force, code_file=code_file) == value


def test_precondition_assignment_form_is_name_matched_not_value_matched():
    """Documents why the bare-value form above is the honest test."""
    from agent.redact import redact_sensitive_text
    out = redact_sensitive_text(ENV_BODY, force=True, code_file=False)
    assert YELP_VALUE not in out      # matched via API_KEY in the name
    assert OPQ_VALUE not in out       # matched via SECRET in the name
    assert PARTNER_VALUE in out       # unremarkable name -> NOT matched


# =============================================================================
# Direction 2 — the corrected behaviour, through the real tool paths
# =============================================================================

@pytest.mark.parametrize("value", ALL_CANARIES)
def test_read_file_returns_no_canary(env_file, value):
    from tools.file_tools import read_file_tool
    out = read_file_tool(str(env_file))
    assert_absent(out, value, "read_file")
    assert json.loads(out).get("error")


@pytest.mark.parametrize("value", ALL_CANARIES)
def test_search_files_returns_no_canary(env_file, value):
    from tools.file_tools import search_tool
    out = search_tool(pattern="CANARY|Zq7Z|Rk9W", path=str(env_file.parent),
                      output_mode="content")
    assert_absent(out, value, "search_files")


@pytest.mark.parametrize("value", ALL_CANARIES)
def test_patch_miss_hint_returns_no_canary(env_file, value):
    from tools.file_tools import patch_tool
    out = patch_tool(mode="replace", path=str(env_file),
                     old_string="absent-sentinel", new_string="x")
    assert_absent(out, value, "patch did-you-mean snippet")


@pytest.mark.parametrize("value", ALL_CANARIES)
def test_read_file_raw_returns_no_canary(env_file, value):
    from tools.file_tools import _get_file_ops
    result = _get_file_ops().read_file_raw(str(env_file))
    assert_absent(str(result), value, "read_file_raw")


@pytest.mark.parametrize("value", ALL_CANARIES)
def test_terminal_style_output_is_scrubbed_at_dispatch(env_file, value, monkeypatch):
    """`cat .env` through the tool dispatcher (the surface layer 1 cannot see)."""
    monkeypatch.setenv("HERMES_HOME", str(env_file.parent))
    ct.reset_cache()

    from tools.registry import ToolRegistry
    reg = ToolRegistry()
    reg.register(
        name="terminal", toolset="test",
        schema={"name": "terminal", "description": "", "input_schema": {"type": "object"}},
        handler=lambda args, **kw: json.dumps({"output": ENV_BODY}),
        check_fn=lambda: True,
    )
    out = reg.dispatch("terminal", {"command": f"cat {env_file}"})
    assert_absent(out, value, "terminal via dispatch")


# --- the template carve-out must survive all of this ------------------------

def test_env_example_remains_readable(tmp_path):
    template = tmp_path / ".env.example"
    template.write_text(f"{YELP_KEY}=\n{OPQ_KEY}=\n{PARTNER_KEY}=\n")
    from tools.file_tools import read_file_tool
    out = read_file_tool(str(template))
    assert not json.loads(out).get("error")
    assert YELP_KEY in out
