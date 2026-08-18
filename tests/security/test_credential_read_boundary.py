"""Layer 1 end-to-end: credential files cannot be read through the tools
(Phase 9 / Packet C, C3/C4).

The canaries are synthetic. Both are exercised in TWO presentations:

  assignment form  YELP_API_KEY=<v>        -- what `cat .env` emits
  bare-value form  <v>                     -- what `cut -d= -f2 .env` emits

The distinction matters. _ENV_ASSIGN_RE (agent/redact.py:105) keys off the
VARIABLE NAME, so OPAQUE_INTERNAL_SECRET=<v> matches via "SECRET" no matter how
opaque the value is. Only the bare-value form proves the real claim: that no
content pattern anywhere in agent/redact.py can save us, and therefore that
path-based denial is doing the work.
"""

import json
import os

import pytest

from agent.file_safety import get_read_block_error

YELP_KEY = "YELP_API_KEY"
OPQ_KEY = "OPAQUE_INTERNAL_SECRET"

# Known vendor shape: long URL-safe base64, recognisable to a human.
YELP_VALUE = "CANARYyelp_Bx7Qm2Zt9Kd4Rp1Wn6Fj3Hs8Lv5Gc0Ty-_Ee4Uu8Ii2Oo6Aa1"
# Opaque: high entropy, no vendor prefix, no delimiter, matches nothing.
OPQ_VALUE = "Zq7Z4mKp2Wf9Lx3Rv8Tn1Yb6Hd5Gs0Jc"

ENV_BODY = f"{YELP_KEY}={YELP_VALUE}\n{OPQ_KEY}={OPQ_VALUE}\n"
CANARY_VALUES = [YELP_VALUE, OPQ_VALUE]


def assert_no_canary(blob: str, note: str = ""):
    for value in CANARY_VALUES:
        assert value not in blob, f"canary leaked{(' — ' + note) if note else ''}: {value[:12]}..."


@pytest.fixture
def env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text(ENV_BODY)
    return f


@pytest.fixture
def template_file(tmp_path):
    f = tmp_path / ".env.example"
    f.write_text(f"{YELP_KEY}=\n{OPQ_KEY}=\n")
    return f


# --- read_file ---------------------------------------------------------------

def test_read_file_refuses_env(env_file):
    from tools.file_tools import read_file_tool
    out = read_file_tool(str(env_file))
    assert_no_canary(out, "read_file")
    assert json.loads(out).get("error")


def test_read_file_refuses_env_regardless_of_redaction_preference(env_file, monkeypatch):
    """The requirement: enforcement must not depend on the user's preference."""
    monkeypatch.delenv("HERMES_REDACT_SECRETS", raising=False)
    import importlib

    import agent.redact as redact
    importlib.reload(redact)
    assert redact._REDACT_ENABLED is False, "precondition: redaction is OFF"

    from tools.file_tools import read_file_tool
    out = read_file_tool(str(env_file))
    assert_no_canary(out, "read_file with redaction disabled")
    assert json.loads(out).get("error")


def test_read_file_still_reads_the_template(template_file):
    from tools.file_tools import read_file_tool
    out = read_file_tool(str(template_file))
    assert not json.loads(out).get("error"), "template must stay readable"
    assert YELP_KEY in out


def test_read_file_still_reads_ordinary_source(tmp_path):
    src = tmp_path / "main.py"
    src.write_text("print('hello')\n")
    from tools.file_tools import read_file_tool
    out = read_file_tool(str(src))
    assert not json.loads(out).get("error")
    assert "hello" in out


# --- search_files ------------------------------------------------------------

# --- ACP shim ----------------------------------------------------------------

def test_acp_shim_guard_fires(env_file):
    """agent/copilot_acp_client.py raises PermissionError on a truthy result."""
    assert get_read_block_error(str(env_file)) is not None


def test_hub_cache_block_still_works(tmp_path, monkeypatch):
    """Regression: chaining must not break the original anti-injection block."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    hub = tmp_path / "skills" / ".hub" / "index-cache"
    hub.mkdir(parents=True)
    target = hub / "catalog.json"
    target.write_text("{}")
    msg = get_read_block_error(str(target))
    assert msg is not None
    assert "prompt injection" in msg
