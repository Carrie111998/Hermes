"""Layer 1 at the byte layer: file_operations (Phase 9 / Packet C, C4).

Split from test_credential_read_boundary.py so each commit is independently
green: C3 wires the tool-level chokepoint, C4 wires the byte layer beneath it
(which is what also closes patch / patch_parser / the fuzzy-match snippet).
"""

import pytest

from tests.security.test_credential_read_boundary import (
    ENV_BODY,
    YELP_KEY,
    assert_no_canary,
)


@pytest.fixture
def env_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text(ENV_BODY)
    return f


def test_search_files_does_not_return_env_line_content(tmp_path):
    (tmp_path / ".env").write_text(ENV_BODY)
    (tmp_path / ".env.example").write_text(f"{YELP_KEY}=\n")
    (tmp_path / "app.py").write_text("# CANARYmarker in ordinary source\n")

    from tools.file_tools import search_tool
    out = search_tool(pattern="CANARY", path=str(tmp_path), output_mode="content")
    assert_no_canary(out, "search_files")


# --- patch / fuzzy-match snippet ---------------------------------------------

def test_patch_miss_hint_does_not_echo_env_content(env_file):
    """patch echoes real file content in its 'did you mean' snippet."""
    from tools.file_tools import patch_tool
    out = patch_tool(mode="replace", path=str(env_file),
                     old_string="definitely-not-present", new_string="x")
    assert_no_canary(out, "patch miss-hint")


# --- byte layer --------------------------------------------------------------

def test_read_file_raw_refuses_env(env_file):
    from tools.file_tools import _get_file_ops
    result = _get_file_ops().read_file_raw(str(env_file))
    assert_no_canary(str(result), "read_file_raw")
    assert result.error


def test_read_file_backend_refuses_env(env_file):
    from tools.file_tools import _get_file_ops
    result = _get_file_ops().read_file(str(env_file))
    assert_no_canary(str(result), "ShellFileOperations.read_file")
    assert result.error
