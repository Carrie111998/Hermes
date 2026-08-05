# Surrogateescape-Safe File Writes — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `write_file` round-trip surrogateescape-decoded content through the real `LocalEnvironment` pipe (preserving original bytes, byte count, and SHA-256) and reject unencodable lone surrogates synchronously before any child process spawns — with no hangs and no false persistence success (issue #79178).

**Architecture:** One byte representation — `utf-8` + `surrogateescape` (the exact inverse of the decode that produces surrogate content) — is used consistently for stdin transmission, byte count, and SHA-256. The stdin writer thread is hardened to always close stdin (`finally`) and capture encoding failures onto the Popen object; `_wait_for_process` surfaces captured failures as a `stdin_error` result key; `write_file` rejects lone surrogates with a regex scan before any subprocess. A one-word sibling fix covers the background-PTY writer.

**Tech Stack:** Python 3.11, stdlib only (`subprocess`, `threading`, `re`, `hashlib`). Tests: `pytest` + real `LocalEnvironment` subprocesses (no new dependencies, no network).

## Global Constraints

Every task inherits these — from AGENTS.md and `docs/surrogateescape-write-path.md` (the spec — read it first):

- **Test runner:** ALWAYS use `scripts/run_tests.sh <file> -k <test> -q` from the repo root — never bare `pytest` (CI parity: credential-free env, UTC, isolated `HERMES_HOME`). The runner is file-granular; `-k` selects a single test.
- **No new dependencies.** Do not touch `pyproject.toml` / `uv.lock`. Stdlib + pytest + `unittest.mock` only.
- **The byte contract:** intended bytes == transmitted bytes == on-disk bytes == hashed bytes, all via `utf-8` + `surrogateescape`. `surrogatepass` is wrong (not the inverse of the decode) and must be removed from the hash block.
- **Reject, don't mangle:** content with surrogates outside U+DC80–U+DCFF (e.g. `"\ud800"`, `"\udc7f"`, `"\udd00"`) is refused synchronously in `write_file` before any subprocess — error text must contain "NOT created or modified".
- **Writer thread ordering is load-bearing:** resolve the stdin target BEFORE encoding; record errors BEFORE closing stdin; ALWAYS close stdin in `finally`. A failure to close = the child hangs = the bug we are fixing.
- **Scope:** POSIX pipe backends (local, ssh, docker, singularity via `_pipe_stdin`/`_popen_bash`). Do NOT touch modal/daytona heredoc/payload transports.
- **No drive-by refactors.** Touch only the lines each task names.
- **Commit convention:** conventional commits (`fix(scope): message`), one commit per task, tests + implementation together.
- **Reference repro** (should fail today, pass after Task 3): `ops.write_file("surrogate.bin", b"\xff".decode("utf-8", "surrogateescape"))` → currently `UnicodeEncodeError` in the writer thread + 30s hang + misleading "Terminated [Command timed out]".

---

### Task 1: Harden `_pipe_stdin` — surrogateescape, always-close, error capture

**Files:**
- Modify: `tools/environments/base.py:269-300` (`_pipe_stdin`)
- Create: `tests/tools/test_file_write_surrogate_roundtrip.py` (real-subprocess unit tests for `_pipe_stdin`)

**Interfaces:**
- Consumes: existing `_pipe_stdin(proc: subprocess.Popen, data: str) -> None`.
- Produces: `proc._hermes_stdin_errors` — a `list[BaseException]` attached to the Popen object **before** the writer thread starts (empty list = no failure). Task 2 reads this attribute; the `_hermes_` attr convention matches existing `proc._hermes_pgid` in `local.py`.

- [ ] **Step 1: Write the failing tests**

Create `tests/tools/test_file_write_surrogate_roundtrip.py` with exactly this content:

```python
"""Surrogate-safe stdin piping for the local execution environment (#79178).

These tests exercise the REAL `_pipe_stdin` writer thread against a real
subprocess — no mocks. They pin the round-trip byte contract (utf-8 +
surrogateescape is the inverse of the decode that produced the content) and
the always-close / error-capture guarantees of the writer thread. Later
tasks in this plan append propagation and write_file tests to this file.
"""
import shlex
import subprocess

import pytest

from tools.environments.base import _pipe_stdin


def _cat_to_file_proc(out_path):
    """A real child that copies its stdin to a file, byte for byte."""
    return subprocess.Popen(
        ["bash", "-c", f"cat > {shlex.quote(str(out_path))}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def _wait_or_kill(proc, timeout=5):
    """wait() with a bounded timeout; kill on timeout so a hung child never
    leaks into the next test."""
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise


class TestPipeStdinSurrogates:
    def test_roundtrips_surrogateescape_bytes(self, tmp_path):
        out = tmp_path / "out.bin"
        proc = _cat_to_file_proc(out)
        content = b"\xff\x00\xfe".decode("utf-8", "surrogateescape")
        try:
            _pipe_stdin(proc, content)
            _wait_or_kill(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert proc.returncode == 0
        assert out.read_bytes() == b"\xff\x00\xfe"
        assert proc._hermes_stdin_errors == []

    def test_unencodable_surrogate_captures_error_and_closes_stdin(self, tmp_path):
        out = tmp_path / "out.bin"
        proc = _cat_to_file_proc(out)
        try:
            _pipe_stdin(proc, "\ud800")  # outside the surrogateescape round-trip range
            _wait_or_kill(proc)  # child MUST exit promptly — stdin closed in finally
        finally:
            if proc.poll() is None:
                proc.kill()
        assert proc.returncode == 0  # child saw EOF and exited cleanly
        assert proc._hermes_stdin_errors  # the encode failure was captured
        assert isinstance(proc._hermes_stdin_errors[0], UnicodeEncodeError)

    def test_normal_content_unchanged(self, tmp_path):
        out = tmp_path / "out.bin"
        proc = _cat_to_file_proc(out)
        try:
            _pipe_stdin(proc, "hello\nworld\n")
            _wait_or_kill(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert proc.returncode == 0
        assert out.read_bytes() == b"hello\nworld\n"
        assert proc._hermes_stdin_errors == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -q`
Expected: `test_roundtrips_surrogateescape_bytes` FAILS (the strict encode kills the writer thread → stdin is never closed → the child hangs → `_wait_or_kill` raises `TimeoutExpired`, and `out.bin` is empty/missing); `test_unencodable_surrogate_captures_error_and_closes_stdin` FAILS (same hang and/or `AttributeError: 'Popen' object has no attribute '_hermes_stdin_errors'`); `test_normal_content_unchanged` PASSES (strict UTF-8 is fine for normal content — proves the tests are otherwise sound).

- [ ] **Step 3: Write the minimal implementation**

Replace the body of `_pipe_stdin` in `tools/environments/base.py:269-300` with:

```python
def _pipe_stdin(proc: subprocess.Popen, data: str) -> None:
    """Write *data* to proc.stdin on a daemon thread to avoid pipe-buffer deadlocks.

    On Windows, text-mode stdin (``text=True`` / ``encoding="utf-8"``)
    translates ``\\n`` → ``\\r\\n`` as the data flows through the pipe —
    which corrupts every write_file / patch call because the bytes that
    land on disk include injected carriage returns.  The file IS created,
    but every subsequent byte-count / content compare against the
    caller's ``\\n``-only string fails.

    Workaround: write through ``proc.stdin.buffer`` (the underlying byte
    buffer), encoding to UTF-8 ourselves.  That bypasses Python's
    newline translation entirely on every platform.  No behaviour change
    on POSIX — the byte sequence is identical to what text-mode would
    produce there.

    Encoding uses ``errors="surrogateescape"`` — the exact inverse of the
    ``surrogateescape`` decode that produced surrogate-bearing content, so
    the original bytes are restored.  For surrogate-free strings this is
    byte-identical to strict UTF-8.  Surrogates outside the round-trip
    range (U+DC80–U+DCFF) raise; the exception is recorded on
    ``proc._hermes_stdin_errors`` and stdin is still closed in ``finally``
    so the child sees EOF instead of hanging.  ``_wait_for_process`` reads
    the recorded error and surfaces it as ``stdin_error`` on the result.
    """

    errors: list[BaseException] = []
    proc._hermes_stdin_errors = errors

    def _write():
        if proc.stdin is None:
            errors.append(RuntimeError("process stdin unavailable"))
            return
        # Resolve the target BEFORE encoding: a failed encode must still
        # reach the finally-close, or the child hangs on EOF forever.
        target = getattr(proc.stdin, "buffer", proc.stdin)
        try:
            raw = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data
            written = target.write(raw)
            if written != len(raw):
                # Buffered writers normally complete or raise; a short write
                # is a real failure and must be surfaced, not swallowed.
                raise RuntimeError(f"short stdin write: {written} of {len(raw)} bytes")
        except (BrokenPipeError, OSError):
            pass  # child closed stdin early — normal
        except Exception as exc:
            # Only reachable with surrogates outside the surrogateescape
            # round-trip range (e.g. a literal U+D800). Record it so
            # _wait_for_process can surface it instead of a silent false
            # success.
            errors.append(exc)
        finally:
            try:
                target.close()
            except Exception:
                pass

    threading.Thread(target=_write, daemon=True).start()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -q`
Expected: 3 PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/tools/test_file_write_surrogate_roundtrip.py tools/environments/base.py
git commit -m "fix(environments): surrogateescape-safe stdin piping, always close stdin (#79178)"
```

---

### Task 2: Surface stdin write failures — `_wait_for_process` + `_exec`

**Files:**
- Modify: `tools/environments/base.py` — `_pipe_stdin` (store the writer-thread handle: `proc._hermes_stdin_thread = thread` before `thread.start()`), `_wait_for_process` natural-exit path (currently line 1210)
- Modify: `tools/file_operations.py` — `ShellFileOperations._exec` (lines 846-876, mapping at 872-876)
- Test: extend `tests/tools/test_file_write_surrogate_roundtrip.py` (append classes below)

**Interfaces:**
- Consumes: `proc._hermes_stdin_errors` (Task 1) and `proc._hermes_stdin_thread` (added here to `_pipe_stdin`).
- Produces: `"stdin_error": str` key on `BaseEnvironment.execute()` result dicts (only when the writer thread failed and the child still exited); `ShellFileOperations._exec` maps a `stdin_error` on an otherwise-zero returncode to `exit_code=1`.

**Review carry-forward (codex quality review of Task 1):** a child that exits WITHOUT reading stdin (e.g. `bash -c 'exit 0'`) can let the writer thread finish after `_wait_for_process` reads the error list — a legit encode failure could be silently missed. `_wait_for_process` MUST join the writer thread (bounded) before reading `_hermes_stdin_errors`. The writer thread cannot block long after child exit (write raises `BrokenPipeError` once the pipe closes), so `join(timeout=5)` is a pure safety net.

- [ ] **Step 1: Write the failing tests**

Append to `tests/tools/test_file_write_surrogate_roundtrip.py` (add `import time` to the imports, and add `from unittest.mock import MagicMock`, `from tools.environments.local import LocalEnvironment`, `from tools.file_operations import ShellFileOperations`):

```python
@pytest.fixture
def env(tmp_path):
    """A real LocalEnvironment rooted in a temp directory."""
    return LocalEnvironment(cwd=str(tmp_path), timeout=15)


class TestStdinErrorPropagation:
    def test_execute_surfaces_stdin_error_without_hanging(self, env):
        t0 = time.monotonic()
        result = env.execute("cat > /dev/null", stdin_data="\ud800")
        elapsed = time.monotonic() - t0

        assert result["returncode"] == 0  # child saw EOF, exited cleanly
        assert result.get("stdin_error")  # the write failure was surfaced
        assert "stdin write failed" in result["output"]
        assert elapsed < 5.0, f"stdin failure path hung for {elapsed:.1f}s"


class TestExecStdinErrorMapping:
    def test_exec_maps_stdin_error_to_failure(self):
        """Defense-in-depth path (unreachable from write_file after Task 3 —
        tested with a mock env for exactly that reason)."""
        env = MagicMock()
        env.execute.return_value = {
            "output": "child output\n",
            "returncode": 0,
            "stdin_error": "boom",
        }
        ops = ShellFileOperations(env, cwd="/tmp")
        result = ops._exec("echo hi", cwd="/tmp", stdin_data="\ud800")
        assert result.exit_code == 1
        assert "boom" in result.stdout


class TestPipeStdinRemainingBranches:
    """Review-requested coverage: bytes passthrough + proc.stdin None."""

    def test_bytes_input_passes_through_untouched(self, tmp_path):
        out = tmp_path / "out.bin"
        proc = subprocess.Popen(
            ["bash", "-c", f"cat > {shlex.quote(str(out))}"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        try:
            _pipe_stdin(proc, b"\x00\x01\xfe")
            _wait_or_kill(proc)
        finally:
            if proc.poll() is None:
                proc.kill()
        assert proc.returncode == 0
        assert out.read_bytes() == b"\x00\x01\xfe"
        assert proc._hermes_stdin_errors == []

    def test_stdin_none_records_runtime_error(self, tmp_path):
        proc = subprocess.Popen(
            ["bash", "-c", "exit 0"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace",
        )
        _pipe_stdin(proc, "data")
        _wait_or_kill(proc)
        assert proc.returncode == 0
        assert proc._hermes_stdin_errors
        assert isinstance(proc._hermes_stdin_errors[0], RuntimeError)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -k "TestStdinErrorPropagation or TestExecStdinErrorMapping or TestPipeStdinRemainingBranches" -q`
Expected: `test_execute_surfaces_stdin_error_without_hanging` FAILS (`result.get("stdin_error")` is None, message absent); `test_exec_maps_stdin_error_to_failure` FAILS (exit_code is 0); the two `TestPipeStdinRemainingBranches` tests PASS (they pin Task 1's already-landed behavior — soundness controls, not red tests).

- [ ] **Step 3: Write the minimal implementation**

In `tools/environments/base.py::_pipe_stdin`, change the thread start (currently `threading.Thread(target=_write, daemon=True).start()`) to store the handle before starting:

```python
    thread = threading.Thread(target=_write, daemon=True)
    proc._hermes_stdin_thread = thread
    thread.start()
```

In `tools/environments/base.py::_wait_for_process`, replace the natural-exit return (currently `return self._finalize_wait_result(output, output.render(), proc.returncode)` at line 1210) with:

```python
        # Join the stdin writer thread before reading its error list: a child
        # that exits without reading stdin can otherwise race ahead of a
        # recorded encode failure, silently dropping it. The thread cannot
        # block long after child exit (write raises BrokenPipeError once the
        # pipe closes); the timeout is a pure safety net.
        stdin_thread = getattr(proc, "_hermes_stdin_thread", None)
        if stdin_thread is not None:
            stdin_thread.join(timeout=5)
        rendered = output.render()
        result = self._finalize_wait_result(output, rendered, proc.returncode)
        stdin_errors = getattr(proc, "_hermes_stdin_errors", None)
        if stdin_errors:
            err = str(stdin_errors[0])
            result["stdin_error"] = err
            result["output"] = rendered + f"\n[stdin write failed: {err}]"
        return result
```

(The child's real `returncode` is preserved; the message is appended to whatever output the child produced, so it is visible even when the child wrote nothing. `_finalize_wait_result`'s signature only takes collector/rendered/returncode, so the error is folded in after the call.)

In `tools/file_operations.py`, replace the result-mapping tail of `_exec` (lines 872-876) with:

```python
        result = self.env.execute(command, cwd=effective_cwd, **kwargs)
        exit_code = result.get("returncode", 0)
        # A stdin write failure with an otherwise-clean child exit is still
        # a failure: the child never received the intended input. write_file
        # rejects such content up front (Task 3); this mapping is
        # defense-in-depth for any other stdin caller.
        if result.get("stdin_error") and exit_code == 0:
            exit_code = 1
        return ExecuteResult(
            stdout=result.get("output", ""),
            exit_code=exit_code
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -k "TestStdinErrorPropagation or TestExecStdinErrorMapping" -q`
Expected: 2 PASS. Also re-run the full file: `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -q` — all 5 PASS (Task 1 tests must not regress).

- [ ] **Step 5: Commit**

```bash
git add tools/environments/base.py tools/file_operations.py tests/tools/test_file_write_surrogate_roundtrip.py
git commit -m "fix(environments): surface stdin write failures as stdin_error (#79178)"
```

---

### Task 3: Reject lone surrogates early + fix the hash codec in `write_file`

**Files:**
- Modify: `tools/file_operations.py` — `write_file` (early rejection after the denied-path check ~line 1459; post-BOM encode before `_atomic_write` ~line 1585; hash block lines 1590-1599)
- Test: extend `tests/tools/test_file_write_surrogate_roundtrip.py` (append class below)

**Interfaces:**
- Consumes: Tasks 1-2 (pipe round-trip + propagation).
- Produces: `WriteResult.error` containing "NOT created or modified" for any content with a lone surrogate (checked BEFORE the syntax gate, BOM probes, or any subprocess); `WriteResult(verified=True, bytes_written=N)` with N matching on-disk bytes for surrogateescape content; `bytes_written` and the SHA-256 computed from one pre-computed `content_bytes` (`utf-8` + `surrogateescape`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/tools/test_file_write_surrogate_roundtrip.py` (add the `ops` fixture next to `env`):

```python
@pytest.fixture
def ops(env, tmp_path):
    """ShellFileOperations wired to the real local environment."""
    return ShellFileOperations(env, cwd=str(tmp_path))


class TestWriteFileSurrogates:
    def test_roundtrip_preserves_bytes_count_and_hash(self, ops, tmp_path):
        p = tmp_path / "surrogate.bin"
        res = ops.write_file(str(p), b"\xff\x00\xfe".decode("utf-8", "surrogateescape"))
        assert res.error is None
        assert res.bytes_written == 3
        assert res.verified is True
        assert p.read_bytes() == b"\xff\x00\xfe"
        assert not list(tmp_path.glob(".hermes-tmp*"))

    def test_roundtrip_mixed_normal_and_surrogate(self, ops, tmp_path):
        content = "head\n" + b"\xff".decode("utf-8", "surrogateescape") + "\ntail\n"
        p = tmp_path / "mixed.bin"
        res = ops.write_file(str(p), content)
        assert res.error is None
        assert res.verified is True
        assert p.read_bytes() == b"head\n\xff\ntail\n"

    @pytest.mark.parametrize("bad", ["\ud800", "\udc7f", "\udd00"])
    def test_unencodable_surrogate_rejected_before_write(self, ops, tmp_path, bad):
        p = tmp_path / "reject.bin"
        res = ops.write_file(str(p), bad)
        assert res.error and "surrogate" in res.error
        assert "NOT created or modified" in res.error
        assert "timed out" not in res.error
        assert not p.exists()

    def test_rejected_write_leaves_existing_target_unchanged(self, ops, tmp_path):
        p = tmp_path / "keep.bin"
        p.write_bytes(b"precious original bytes")
        res = ops.write_file(str(p), "\ud800")
        assert res.error and "NOT created or modified" in res.error
        assert p.read_bytes() == b"precious original bytes"

    def test_patch_replace_funnel_rejects_surrogate_new_string(self, ops, tmp_path):
        p = tmp_path / "patchme.txt"
        p.write_text("old\n")
        res = ops.patch_replace(str(p), "old", "new" + "\udcff")
        assert res.error and "surrogate" in res.error
        assert p.read_text() == "old\n"

    def test_normal_content_verified(self, ops, tmp_path):
        p = tmp_path / "normal.txt"
        res = ops.write_file(str(p), "hello\nworld\n")
        assert res.error is None
        assert res.verified is True
        assert p.read_bytes() == b"hello\nworld\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -k TestWriteFileSurrogates -q`
Expected: ALL FAIL. The two round-trip tests fail because the hash still uses `surrogatepass` (`verified` is False → "Post-write verification failed" error). The rejection tests fail because `"\ud800"` is not rejected up front — it reaches the pipe, gets surfaced via `stdin_error`, and **the existing-target test fails worst**: the child receives empty stdin, writes an empty temp file, and `mv` truncates `keep.bin` to 0 bytes (this is exactly why the early rejection must exist).

- [ ] **Step 3: Write the minimal implementation**

In `tools/file_operations.py::write_file`:

(a) Insert the early rejection immediately after the denied-path check (`denied = get_write_denied_error(path)` block, ~line 1459) — BEFORE the fail-closed syntax gate:

```python
        # Reject lone surrogates up front with a regex scan (no encode, no
        # subprocess). surrogateescape-decoded content (U+DC80–U+DCFF)
        # round-trips through the pipe fine, but surrogates outside that
        # range cannot be encoded at all — and letting them reach the pipe
        # would spawn a child that then hangs, or truncates the target via
        # empty-stdin `cat`. Refuse synchronously before any subprocess.
        # NOTE: the range deliberately excludes U+DC80–U+DCFF (the
        # surrogateescape round-trip range) — a naive [\ud800-\udfff] would
        # reject round-trippable content.
        m = re.search(r"[\ud800-\udc7f\udd00-\udfff]", content)
        if m:
            return WriteResult(
                error=(
                    f"Refusing to write '{path}': content contains a lone "
                    f"surrogate character ({m.group(0)!r}) that cannot be "
                    "encoded as UTF-8. The file was NOT created or modified."
                )
            )
```

(`re` is already imported at `tools/file_operations.py:29`.)

(b) Insert the post-BOM encode immediately before `write_result = self._atomic_write(path, content)` (~line 1585) — i.e. AFTER the BOM-prepend at line 1550-1551 so a restored BOM is included in the hash:

```python
        # Encode once for byte count + sha256. surrogateescape is the exact
        # inverse of the decode that may have produced this content, so these
        # are the bytes the pipe transmits and the bytes on disk. The early
        # rejection above guarantees this cannot raise; the try/except is
        # defense for future callers that bypass it.
        try:
            content_bytes = content.encode("utf-8", "surrogateescape")
        except UnicodeEncodeError as exc:
            return WriteResult(
                error=(
                    f"Refusing to write '{path}': content contains a lone "
                    f"surrogate character ({exc}) that cannot be encoded as "
                    "UTF-8. The file was NOT created or modified."
                )
            )
```

(c) Replace the hash block (lines 1590-1599) — delete the `surrogatepass` encode and use the pre-computed bytes:

```python
        # Bytes written — computed from the exact bytes we just wrote (len
        # matches wc -c) instead of spawning a ``wc -c`` subprocess. The
        # encode happened up front with surrogateescape — the inverse of the
        # decode that produces surrogate content — so content_bytes == what
        # rode stdin == what is on disk, and the sha256 below compares like
        # with like.
        bytes_written = len(content_bytes)
```

(Line 1614 `expected_sha = hashlib.sha256(content_bytes).hexdigest()` already uses `content_bytes` — unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -q`
Expected: ALL PASS (Task 1-3 tests, 11 total: 3 pipe + 2 propagation + 6 write). Also run the existing write-path suites to confirm no regression:

```bash
scripts/run_tests.sh tests/tools/test_file_tools_live.py -q
scripts/run_tests.sh tests/tools/test_file_operations.py -q
scripts/run_tests.sh tests/tools/test_write_file_syntax_gate.py -q
scripts/run_tests.sh tests/tools/test_file_write_safety.py -q
```

- [ ] **Step 5: Commit**

```bash
git add tools/file_operations.py tests/tools/test_file_write_surrogate_roundtrip.py
git commit -m "fix(file_operations): reject unencodable surrogates early, hash with surrogateescape (#79178)"
```

---

### Task 4: Sibling fix — background-PTY stdin (`process_registry`)

**Files:**
- Modify: `tools/process_registry.py:1749` (one-word change)
- Create: `tests/tools/test_process_registry_write_stdin_surrogates.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `write_stdin(session_id, data)` returns `{"status": "ok", ...}` for surrogateescape content instead of `{"status": "error", ...}` (the PTY branch at 1742-1753).

- [ ] **Step 1: Write the failing test**

Create `tests/tools/test_process_registry_write_stdin_surrogates.py`:

```python
"""Sibling regression test for #79178: background-PTY stdin must round-trip
surrogateescape content instead of crashing on the strict UTF-8 encode."""
import shlex
import time

import pytest

from tools.process_registry import ProcessRegistry


def test_write_stdin_pty_surrogateescape_roundtrip(tmp_path):
    registry = ProcessRegistry()
    out = tmp_path / "out.bin"
    script = tmp_path / "read_stdin.py"
    # readline(): a PTY never delivers EOF, so read one line (canonical mode
    # delivers it after the newline we send).
    script.write_text(
        f"import sys\nopen({str(out)!r}, 'wb').write(sys.stdin.buffer.readline())\n"
    )
    session = registry.spawn_local(
        f"python3 {shlex.quote(str(script))}",
        cwd=str(tmp_path),
        use_pty=True,
    )
    if session._pty is None:
        registry.kill_process(session.id)
        pytest.skip("ptyprocess not available; PTY path not exercised")
    try:
        result = registry.write_stdin(
            session.id, b"\xff".decode("utf-8", "surrogateescape") + "\n"
        )
        assert result["status"] == "ok", result
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not out.exists():
            time.sleep(0.05)
        assert out.read_bytes() == b"\xff\n"
    finally:
        registry.kill_process(session.id)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `scripts/run_tests.sh tests/tools/test_process_registry_write_stdin_surrogates.py -q`
Expected: FAIL — `write_stdin` returns `{"status": "error", "error": "'utf-8' codec can't encode character '\\udcff' ..."}` (strict encode raises inside the try at `process_registry.py:1752`).

- [ ] **Step 3: Write the minimal implementation**

In `tools/process_registry.py:1749`, change:

```python
                    pty_data = data.encode("utf-8") if isinstance(data, str) else data
```

to:

```python
                    # surrogateescape: a PTY is a byte stream — round-trip the
                    # original bytes instead of crashing on surrogate content.
                    pty_data = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data
```

Nothing else in the function changes; the surrounding try/except already returns `{"status": "error", ...}` on failure, so a hang is not possible here.

- [ ] **Step 4: Run test to verify it passes**

Run: `scripts/run_tests.sh tests/tools/test_process_registry_write_stdin_surrogates.py -q`
Expected: PASS. Also run the gateway background-process suites that touch `process_registry`:

```bash
scripts/run_tests.sh tests/gateway/test_background_process_notifications.py -q
scripts/run_tests.sh tests/gateway/test_clean_shutdown_marker.py -q
```

- [ ] **Step 5: Commit**

```bash
git add tools/process_registry.py tests/tools/test_process_registry_write_stdin_surrogates.py
git commit -m "fix(process_registry): surrogateescape-safe PTY stdin writes (#79178)"
```

---

## Final verification

- [ ] Full new-test file green: `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -q`
- [ ] Sibling test green: `scripts/run_tests.sh tests/tools/test_process_registry_write_stdin_surrogates.py -q`
- [ ] Existing write-path suites green (Task 3 Step 4 commands).
- [ ] The issue's repro completes fast and correctly: `ops.write_file("surrogate.bin", b"\xff".decode("utf-8", "surrogateescape"))` → `error is None`, `verified is True`, on-disk bytes `b"\xff"`.
- [ ] `git status` clean except expected files; one commit per task on the branch.
