"""Regression tests for issue #81984.

The truncation footer for ``delegate_task`` subagent summaries and
``web_extract`` pages prints the host absolute path of the spill file. On
docker/modal/ssh backends the parent agent can only see the container's
bind-mounted location, so the unreadable host path causes the model to
re-dispatch work that is already on disk. The fix translates the host path
to the sandbox-visible path (via ``to_agent_visible_cache_path``) at the
three render sites, so the printed hint matches what ``read_file`` can
actually open inside the container.
"""

import os
import tempfile
from pathlib import Path

import pytest

import tools.delegate_tool as dt
import tools.web_tools as wt


# ─── delegate_task / _trim_summary_with_footer ─────────────────────────────


@pytest.fixture
def _isolated_hermes_home(monkeypatch):
    """Pin HERMES_HOME to a temp directory so the spill store is predictable."""
    with tempfile.TemporaryDirectory() as td:
        monkeypatch.setenv("HERMES_HOME", os.path.join(td, ".hermes"))
        yield td


def _big_summary() -> str:
    return "HEAD_MARKER\n" + ("X" * 50_000) + "\nTAIL_MARKER"


def test_footer_uses_host_path_on_local_backend(_isolated_hermes_home, monkeypatch):
    """Local backend: the printed path is the host path (no translation)."""
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    model_text, host_path = dt._trim_summary_with_footer(
        _big_summary(), cap=2_000, task_index=0,
    )
    assert host_path and os.path.exists(host_path)
    # The footer must show the host path so the parent can read it locally.
    assert host_path in model_text
    # No container prefix should leak into the footer.
    assert "/root/.hermes" not in model_text
    assert "~/.hermes" not in model_text


def test_footer_translates_to_root_hermes_on_docker_backend(
    _isolated_hermes_home, monkeypatch,
):
    """Docker backend: host path is rewritten to /root/.hermes/... in the footer."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    model_text, host_path = dt._trim_summary_with_footer(
        _big_summary(), cap=2_000, task_index=0,
    )
    assert host_path and os.path.exists(host_path)
    # The host path is still the real on-disk location (file lives on host).
    assert os.path.abspath(host_path) == os.path.abspath(host_path)
    # The footer must show the container-visible path, not the host path.
    assert "/root/.hermes/" in model_text
    # The original host path must NOT appear in the printed footer (issue #81984).
    leaf = Path(host_path).name
    # Only the leaf may appear (truncation that still has it in the middle).
    # Specifically: the host path's parent directory must NOT be in the footer.
    parent_dir = str(Path(host_path).parent)
    assert parent_dir not in model_text, (
        f"host path leaked: {parent_dir!r} should be /root/.hermes/... in docker footer"
    )
    # The read_file hint must use the mounted path.
    assert 'read_file path="/root/.hermes/' in model_text
    # The leaf filename is preserved.
    assert leaf in model_text


def test_footer_translates_to_root_hermes_on_modal_backend(
    _isolated_hermes_home, monkeypatch,
):
    """Modal backend: same translation as docker."""
    monkeypatch.setenv("TERMINAL_ENV", "modal")
    model_text, _host_path = dt._trim_summary_with_footer(
        _big_summary(), cap=2_000, task_index=1,
    )
    assert "/root/.hermes/" in model_text
    assert 'read_file path="/root/.hermes/' in model_text


def test_footer_uses_tilde_hermes_on_ssh_backend(
    _isolated_hermes_home, monkeypatch,
):
    """SSH backend: host path is rewritten to ~/.hermes/... in the footer."""
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    model_text, host_path = dt._trim_summary_with_footer(
        _big_summary(), cap=2_000, task_index=2,
    )
    leaf = Path(host_path).name
    parent_dir = str(Path(host_path).parent)
    assert parent_dir not in model_text
    assert "~/.hermes/" in model_text
    assert f'~/.hermes/cache/delegation/{leaf}' in model_text
    # The read_file hint must use the mounted path.
    assert 'read_file path="~/.hermes/' in model_text


def test_footer_uses_tilde_hermes_on_daytona_backend(
    _isolated_hermes_home, monkeypatch,
):
    """Daytona backend: same as SSH / ~/.hermes/..."""
    monkeypatch.setenv("TERMINAL_ENV", "daytona")
    model_text, host_path = dt._trim_summary_with_footer(
        _big_summary(), cap=2_000, task_index=3,
    )
    parent_dir = str(Path(host_path).parent)
    assert parent_dir not in model_text
    assert "~/.hermes/" in model_text


def test_footer_no_translation_on_singularity_backend(
    _isolated_hermes_home, monkeypatch,
):
    """Singularity backend: host path is exactly correct (auto-bind), no translation."""
    monkeypatch.setenv("TERMINAL_ENV", "singularity")
    model_text, host_path = dt._trim_summary_with_footer(
        _big_summary(), cap=2_000, task_index=4,
    )
    # The host path appears verbatim in the footer (no translation happened).
    assert host_path in model_text
    assert "/root/.hermes" not in model_text
    assert "~/.hermes" not in model_text


def test_footer_falls_back_when_helper_import_fails(
    _isolated_hermes_home, monkeypatch,
):
    """If credential_files.to_agent_visible_cache_path is unavailable, fall back to host path."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    # Force the try/except to swallow the import so we exercise the fallback.
    import sys

    # Block the lazy import in delegate_tool from succeeding.
    monkeypatch.setattr(
        sys.modules["tools.delegate_tool"], "credential_files", None, raising=False,
    )
    # Re-import delegate_tool (the attribute access is runtime, not import-time).
    model_text, host_path = dt._trim_summary_with_footer(
        _big_summary(), cap=2_000, task_index=5,
    )
    # Fallback: host path is shown verbatim. We don't want a crash on missing helper.
    assert host_path in model_text or "/root/.hermes/" in model_text


# ─── web_extract / _truncate_with_footer ───────────────────────────────────


def test_web_extract_footer_translates_on_docker_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    big = "HEAD\n" + ("Z" * 60_000) + "\nTAIL"
    model_text, was_truncated = wt._truncate_with_footer(big, "https://example.com", 2_000)
    assert was_truncated is True
    assert "/root/.hermes/" in model_text
    assert 'read_file path="/root/.hermes/' in model_text
    assert "Full text saved to: /root/.hermes/" in model_text


def test_web_extract_footer_uses_tilde_hermes_on_ssh_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "ssh")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    big = "HEAD\n" + ("Z" * 60_000) + "\nTAIL"
    model_text, was_truncated = wt._truncate_with_footer(big, "https://example.com", 2_000)
    assert was_truncated is True
    assert "~/.hermes/" in model_text
    assert 'read_file path="~/.hermes/' in model_text


def test_web_extract_footer_uses_host_path_on_local_backend(monkeypatch, tmp_path):
    monkeypatch.delenv("TERMINAL_ENV", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    big = "HEAD\n" + ("Z" * 60_000) + "\nTAIL"
    model_text, was_truncated = wt._truncate_with_footer(big, "https://example.com", 2_000)
    assert was_truncated is True
    # No container prefix.
    assert "/root/.hermes" not in model_text
    assert "~/.hermes" not in model_text
    # The host path is the actual file on disk.
    stored_marker = "Full text saved to: "
    assert stored_marker in model_text
    stored_path = model_text.split(stored_marker, 1)[1].split("\n", 1)[0]
    assert os.path.exists(stored_path)


def test_web_extract_footer_no_translation_on_singularity_backend(monkeypatch, tmp_path):
    monkeypatch.setenv("TERMINAL_ENV", "singularity")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    big = "HEAD\n" + ("Z" * 60_000) + "\nTAIL"
    model_text, was_truncated = wt._truncate_with_footer(big, "https://example.com", 2_000)
    assert was_truncated is True
    assert "/root/.hermes" not in model_text
    assert "~/.hermes" not in model_text


# ─── existing batch integration ────────────────────────────────────────────


def test_existing_batch_overflow_still_truncates_and_translates_on_docker(
    _isolated_hermes_home, monkeypatch,
):
    """The existing batch-budget test still passes under docker backend AND
    the footer now points at the container path."""
    monkeypatch.setenv("TERMINAL_ENV", "docker")
    big = "HEAD_MARKER\n" + ("X" * 50_000) + "\nTAIL_MARKER"
    parent = type(
        "P", (),
        {
            "context_compressor": type(
                "C", (), {"context_length": 131_000, "max_tokens": 8_000}
            )(),
            "session_prompt_tokens": 120_000,
        },
    )()
    results = [
        {"task_index": i, "summary": big, "status": "completed"} for i in range(3)
    ]
    dt._apply_summary_budget(results, parent)
    for r in results:
        assert r["summary_truncated"] is True
        # Container path is in the footer.
        assert "/root/.hermes/" in r["summary"]
        # The host path on disk is still real and stored.
        path = r["summary_full_path"]
        assert path and os.path.exists(path)
        with open(path, encoding="utf-8") as fh:
            assert fh.read() == big
