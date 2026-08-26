"""Regression tests for execute_code output redaction (#95509).

Both execute_code paths (remote and local) used ``code_file=True``, which
skips the ENV-assignment pass — so a script that printed ``.env``-shaped
``KEY=value`` lines passed opaque secret values through verbatim. The
terminal tool already solves this class by switching to ``code_file=False``
for env dumps; ``_redact_exec_output`` applies the same pass to sandbox
stdout/stderr unconditionally. All secret-shaped literals below are
synthetic and non-usable.
"""

from tools.code_execution_tool import _redact_exec_output


def test_env_shaped_dump_is_masked():
    """The reporter's exact failure shape: .env lines leak under code_file=True."""
    out = _redact_exec_output(
        "GITHUB_TOKEN=ghp_MAxxxx\n"
        "GEMINI_API_KEY=AQ.AbC123...\n"
        "GROQ_API_KEY=gsk_W8xyz"
    )
    assert "ghp_MAxxxx" not in out
    assert "AQ.AbC123" not in out
    assert "gsk_W8xyz" not in out
    assert out.count("=") == 3  # keys survive, values masked


def test_opaque_values_without_known_prefix_are_masked():
    """Env-dump values with no recognized token prefix still get masked."""
    out = _redact_exec_output(
        "INTERNAL_SECRET=opaque_random_48char_value_no_known_prefix\n"
        "DB_PASSWORD=hunter2style"
    )
    assert "opaque_random_48char_value_no_known_prefix" not in out
    assert "hunter2style" not in out


def test_programmatic_env_lookups_stay_readable():
    """The #2852 guard: KEY=os.getenv('X') references names, not secrets."""
    out = _redact_exec_output('GITHUB_TOKEN=os.getenv("GITHUB_TOKEN")')
    assert out == 'GITHUB_TOKEN=os.getenv("GITHUB_TOKEN")'


def test_plain_output_without_secret_keywords_is_unchanged():
    """Prose keys and ordinary diagnostics must not be mangled."""
    text = "processed 3 files\nauthor=Smith\ntotal=42\nOK"
    assert _redact_exec_output(text) == text


def test_both_execute_code_paths_use_the_env_pass():
    """The remote and local redaction sites must route through the helper.

    Pins the wiring, not just the helper: grep-level guarantee that neither
    path regresses to a bare ``code_file=True`` call.
    """
    import inspect

    import tools.code_execution_tool as cet

    source = inspect.getsource(cet)
    assert "redact_sensitive_text(stdout_text, code_file=True)" not in source
    assert source.count("_redact_exec_output(") >= 3  # def + remote + local(stdout/stderr)


def test_spilled_stdout_file_is_redacted(tmp_path, monkeypatch):
    """Truncated output spills the FULL text to cache/exec — that file must
    carry redacted text, because the result teaches the model to read_file
    it, and read_file (file_read=True implies code_file=True) would skip
    the ENV-assignment pass on spilled plaintext (#95509 rebased onto the
    spillover pipeline).
    """
    import hermes_constants
    from tools.code_execution_tool import _truncate_stdout_text

    monkeypatch.setattr(
        hermes_constants, "get_hermes_dir",
        lambda *a, **k: tmp_path, raising=True,
    )

    secret_dump = "GITHUB_TOKEN=ghp_MAxxxx\nINTERNAL_SECRET=opaquevalue42\n"
    payload = ("padding line that is not secret\n" * 3000) + secret_dump

    # Production order: redact BEFORE truncation, so the spill file written
    # inside _truncate_stdout_text stores the redacted text.
    redacted = _redact_exec_output(payload)
    inline, metadata = _truncate_stdout_text(redacted)

    spill_path = metadata.get("stdout_spill_path")
    assert spill_path, "expected truncation to spill the full output"
    spilled = open(spill_path, encoding="utf-8").read()
    assert "ghp_MAxxxx" not in spilled
    assert "opaquevalue42" not in spilled
    assert "GITHUB_TOKEN=" in spilled  # keys survive, values masked
    assert "ghp_MAxxxx" not in inline
    assert "opaquevalue42" not in inline


def test_redaction_precedes_truncation_in_spill_paths():
    """Both host-side spill callers must redact before truncating; ordering
    is what keeps the spill file scrubbed, so pin it per function."""
    import inspect

    import tools.code_execution_tool as cet

    for fname in ("_finish_remote_kernel_result", "_execute_remote"):
        src = inspect.getsource(getattr(cet, fname))
        assert "_redact_exec_output(stdout_text)" in src, fname
        assert "_truncate_stdout_text(stdout_text)" in src, fname
        assert (
            src.index("_redact_exec_output(stdout_text)")
            < src.index("_truncate_stdout_text(stdout_text)")
        ), f"{fname}: redaction must run before truncation"


def test_session_kernel_redaction_sites_use_the_env_pass():
    """The session-kernel cell path (code_kernel) joined upstream after this
    fix landed; its stdout/stderr/traceback redaction and the cell-spill
    rewrite must all route through the helper — no bare ``code_file=True``.
    """
    import inspect

    import tools.code_kernel as ck

    source = inspect.getsource(ck)
    assert "redact_sensitive_text(" not in source  # no bare calls bypass the helper
    # stdout + stderr + traceback + cell-spill rewrite
    assert source.count("_redact_exec_output(") >= 4
