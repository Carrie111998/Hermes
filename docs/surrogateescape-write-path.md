# Surrogateescape-safe file writes

**One byte representation for stdin transmission, byte count, and SHA-256 verification — or a deterministic rejection before the child starts.**

`write_file` claims to support strings that carry lone surrogates (produced by a
`surrogateescape` decode of non-UTF-8 bytes), but the real `LocalEnvironment`
stdin path still encodes them strictly as UTF-8. The strict encode raises
`UnicodeEncodeError` inside the stdin writer thread, stdin is never closed (no
`finally`), the child (`cat > tmp`) waits for EOF that never comes, and the
caller gets a misleading 30s-timeout failure instead of a write — or, if the
backend were fixed to transmit, a SHA-256 computed over the wrong bytes. Issue
#79178.

The contract this design establishes: **intended bytes == transmitted bytes ==
on-disk bytes == hashed bytes**, all via `utf-8` + `surrogateescape`, which is
the exact inverse of the `surrogateescape` decode that produced the content.
Content with surrogates *outside* the round-trip range (not U+DC80–U+DCFF) is
rejected synchronously before any child process spawns.

---

## The failure chain

Reproduction (real `LocalEnvironment`, real subprocess):

```python
content = b"\xff".decode("utf-8", "surrogateescape")   # "\udcff"
ops.write_file("surrogate.bin", content)
```

1. `ShellFileOperations.write_file` (`tools/file_operations.py:1412`) computes
   the intended byte count and SHA-256 with `content.encode("utf-8",
   "surrogatepass")` (line 1598) — but only *after* `_atomic_write()` has
   already been called, so the write happens first.
2. `_atomic_write` streams content over stdin; `LocalEnvironment._run_bash`
   hands it to `_pipe_stdin` (`tools/environments/base.py:269`).
3. The writer thread executes `data.encode("utf-8")` (line 293) — strict —
   which raises `UnicodeEncodeError` for `"\udcff"`.
4. `UnicodeEncodeError` is not covered by the `(BrokenPipeError, OSError)`
   handler, and `target.close()` sits inside the `try`, so stdin is never
   closed. The child blocks in `cat > "$tmp"` until `_wait_for_process` kills
   it at the timeout; `write_file` reports a misleading "Failed to write file:
   Terminated [Command timed out]".
5. Even if transmission were fixed, the hash is wrong: for `"\udcff"`,
   `surrogatepass` yields `ed b3 bf` (the UTF-8 encoding of the surrogate
   code point), not `ff` (the original byte recovered by `surrogateescape`).
   `surrogatepass` and `surrogateescape` are different codecs with different
   byte semantics — `surrogatepass` is **not** the inverse of a
   `surrogateescape` decode.

### Byte semantics (verified)

| Content | `encode("utf-8")` | `encode("utf-8","surrogateescape")` | `encode("utf-8","surrogatepass")` |
|---|---|---|---|
| `b"\xff"` → `"\udcff"` | `UnicodeEncodeError` | `b"\xff"` ✓ round-trip | `b"\xed\xb3\xbf"` ✗ |
| `"\ud800"`, `"\udc7f"`, `"\udd00"` | `UnicodeEncodeError` | `UnicodeEncodeError` (unrecoverable) | encodable |

Only U+DC80–U+DCFF is recoverable — the exact range a `surrogateescape` decode
of real bytes produces. Everything else must be rejected, not mangled.

## Why not pass bytes down the whole chain

Encoding once at the top and flowing `bytes` through `execute()` would save one
encode of multi-MB content, but requires widening `stdin_data: str | bytes`
across `BaseEnvironment.execute`, the sudo-stdin merge, heredoc-mode embedding,
and modal's JSON payload transport (which cannot carry `bytes`). That touches
remote backends for a perf nicety the status quo already tolerates: today the
content is encoded twice (hash block + pipe) regardless. The pipe encode is
unavoidable — the string must become bytes to cross the pipe. Keep `str`
flowing; encode with the same codec at both ends.

---

## The fix

### 1. Transmission — `_pipe_stdin` (`tools/environments/base.py:269`)

- `data.encode("utf-8")` → `data.encode("utf-8", "surrogateescape")`. For all
  surrogate-free strings (the overwhelming majority of traffic) this is
  byte-identical to strict UTF-8; for `surrogateescape`-decoded content it
  restores the original bytes. One line fixes local **and** every backend that
  pipes through `_pipe_stdin`/`_popen_bash` (ssh, docker, singularity).
- Restructure `_write()`. **Ordering is load-bearing:** the target must be
  resolved *before* the encode, so a failed encode still reaches the `finally`
  close. (A draft that assigned `target` inside the `try` left `target is None`
  when the encode raised and silently preserved the hang — caught in codex
  review.) Error recording also happens *before* stdin closure, which is what
  lets `_wait_for_process` observe the failure: the child cannot exit `cat`
  until stdin closes, so record → close → child exits → reader sees the error.

  ```python
  def _write():
      if proc.stdin is None:
          errors.append(RuntimeError("process stdin unavailable"))
          return
      target = getattr(proc.stdin, "buffer", proc.stdin)  # resolve BEFORE encode
      try:
          raw = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data
          target.write(raw)
      except (BrokenPipeError, OSError):
          pass  # child closed stdin early — normal
      except Exception as exc:
          # Only reachable with surrogates outside the surrogateescape
          # round-trip range (e.g. a literal U+D800). Record it so
          # _wait_for_process can surface it instead of a silent false success.
          errors.append(exc)
      finally:
          try:
              target.close()
          except Exception:
              pass
  ```

  `BufferedWriter.write` either completes the buffered write or raises — no
  partial-write loop is needed, but the implementation must not silently
  swallow the write's return value without checking it.

- The `errors` list is attached to the proc (`proc._hermes_stdin_errors = errors`)
  **before** the thread starts, so there is no race between the writer thread
  and `_wait_for_process`. Attaching `_hermes_*` attributes to Popen objects has
  precedent in this codebase (`proc._hermes_pgid` in `local.py`).
- `finally` guarantees stdin is always closed: a failed encode becomes EOF for
  the child instead of an infinite wait. `BrokenPipeError`/`OSError` keep their
  current pass semantics.

### 2. Propagation — `_wait_for_process` + `_exec`

- In `_wait_for_process` (`tools/environments/base.py:1210`), after
  `drain_thread.join()` and immediately before the `_finalize_wait_result`
  call (whose signature only takes collector/rendered/returncode, so the
  error is read from the proc and folded into the returned dict after):
  if `proc._hermes_stdin_errors` is non-empty, append
  `[stdin write failed: <exc>]` to the rendered output and add a `"stdin_error"`
  key to the result dict. The child's real `returncode` is preserved. The
  message is appended to whatever output the child produced, so it stays
  visible even when the child wrote nothing.
- In `ShellFileOperations._exec` (`tools/file_operations.py:872`): a
  `stdin_error` on an otherwise-zero returncode maps to a failure
  (`exit_code = 1`), so `_atomic_write`'s existing `exit_code != 0` check
  surfaces a clear error instead of false persistence success. This is
  defense-in-depth — the write path is already protected by the early
  rejection in section 3 — and it covers non-write stdin callers
  (`terminal_tool` background input) which read the message directly from the
  result output.

### 3. Write path — `write_file` (`tools/file_operations.py:1412`)

Two checks, deliberately split:

- **Early synchronous rejection** — immediately after the denied-path check
  (line 1459), *before* the syntax gate, BOM probes, lint baseline, or any
  other subprocess. A C-speed regex scan is enough — rejection does not need
  to encode:

  ```python
  m = re.search(r"[\ud800-\udc7f\udd00-\udfff]", content)
  if m:
      return WriteResult(error=(
          f"Refusing to write '{path}': content contains a lone surrogate "
          f"character ({m.group(0)!r}) that cannot be encoded as UTF-8. The "
          "file was NOT created or modified."
      ))
  ```

  The range deliberately EXCLUDES U+DC80–U+DCFF — the surrogateescape
  round-trip range — which must flow through to the pipe. (A naive
  `[\ud800-\udfff]` would reject round-trippable content and break the
  round-trip contract; caught during implementation.)

  This makes "rejected before any child process spawns" literally true — the
  syntax gate and BOM/line-ending probes run after it, so a surrogate-bearing
  structured file is refused with a predictable error before any parser (or
  `head`/`cat` child) sees it. Synchronous, deterministic, no hang, no
  empty-file truncation (a failed pipe write followed by EOF would otherwise
  let `cat > tmp` create an *empty* file and `mv` it over the target).

- **Post-BOM encode for hash/byte-count** — after the BOM-prepend (line 1551)
  so a restored BOM is included, encode once for `bytes_written` and the
  SHA-256:

  ```python
  content_bytes = content.encode("utf-8", "surrogateescape")
  ```

  The early regex check makes this unreachable for surrogates, but it stays
  inside a try/except returning the same `WriteResult` shape as defense for
  any future caller that bypasses the early check.

- Delete the `surrogatepass` encode at line 1598; use the pre-computed
  `content_bytes` for both `bytes_written` and the SHA-256. One encode serves
  hash + byte count. Rewrite the now-incorrect comment block (1590–1597) to
  state the contract above.
- `patch_replace` funnels through `write_file` (line 1745), so every write
  route shares the same validation.

### 4. Sibling path — `tools/process_registry.py:1749`

Background-process PTY input uses the same strict `data.encode("utf-8")`
pattern. One-word change to `"utf-8", "surrogateescape"`. A PTY is a byte
stream; writing the round-tripped original bytes is strictly more correct, and
the change is a no-op for normal content. The exception there is already caught
and returned as `{"status": "error", ...}` — no hang possible — so no other
change is needed.

The adjacent Popen branch (`process_registry.py:1755`) was examined and is
**not** in scope: non-PTY background processes are spawned with
`stdin=subprocess.DEVNULL` (`process_registry.py:790`), so `write_stdin()`
rejects that route before ever calling `.write()`. `tool_result_storage.py`
also sends content via `env.execute(..., stdin_data=...)`; it is covered by the
shared `_pipe_stdin` change in section 1.

---

## Edge cases

- **Windows:** the `proc.stdin.buffer` newline-translation workaround is
  untouched; `surrogateescape` is a codec and platform-independent.
- **Heredoc/payload-mode remote backends** (modal, daytona): untouched and
  **not claimed as verified here** — their stdin rides SDK-specific transports
  (JSON payloads, remote APIs), not plain POSIX argv. The round-trip guarantee
  applies to the POSIX pipe backends (local, ssh, docker, singularity via
  `_popen_bash`). The hash fix in section 3 still makes the verification agree
  with whatever bytes those backends receive.
- **Normal content:** `surrogateescape` is byte-identical to strict UTF-8 for
  any string without surrogates — zero behavior change for the common path,
  including the fail-closed syntax gate (which now runs *after* the surrogate
  rejection, unchanged for normal content).
- **Bytes input to `_pipe_stdin`:** the existing `isinstance(data, str)` branch
  is preserved; bytes pass through untouched.
- **Empty content / BOM-only content:** `b""` encodes fine; the early regex
  rejection runs before BOM prepend (U+FEFF is not a surrogate, so the
  ordering is safe), and the post-BOM encode includes the restored BOM in the
  hash exactly as it is written.

## Testing

New `tests/tools/test_file_write_surrogate_roundtrip.py`, using the real-env
fixture pattern from `tests/tools/test_file_tools_live.py` (real
`LocalEnvironment` + `ShellFileOperations`, temp cwd, no mocks). Assert stable
fields and substrings — never exact full error strings.

1. **Round-trip:** `b"\xff\x00\xfe"` surrogateescape-decoded → `write_file` →
   on-disk bytes identical, `bytes_written == 3`, `verified is True` (the hash
   match that is broken today), no `.hermes-tmp*` leftovers, completes without
   hanging. Include a **mixed** string too (normal text + embedded surrogate
   chars), not only raw surrogate bytes — proves the two codec paths agree on
   the same byte stream.
2. **Rejection:** `"\ud800"`, `"\udc7f"`, `"\udd00"` → `WriteResult.error`
   mentions "surrogate", new target absent, error does **not** contain
   "timed out" (regression guard for the old hang). Additionally: pre-write a
   valid file to the same path, attempt a surrogate write over it, and assert
   the existing target is **byte-for-byte unchanged** — the stronger safety
   property (no truncation, no partial write).
3. **Propagation:** `env.execute("cat > /dev/null", stdin_data="\ud800")` →
   result carries `stdin_error` **and** the output contains the "stdin write
   failed" message, `cat` exits 0 (proves stdin closed in `finally`), and the
   call returns well under the environment timeout (bound elapsed time, e.g.
   < 5s, so a broken implementation fails fast instead of slowly timing out).
4. **Focused `_pipe_stdin` unit test:** a real `Popen` of
   `bash -c 'cat > /dev/null'` with `stdin=PIPE`, `_pipe_stdin(proc,
   "\ud800")`, `proc.wait(timeout=5)` → child exits 0 and
   `proc._hermes_stdin_errors` is non-empty. This directly pins the
   writer-thread ordering (target resolved before encode; error recorded
   before close) that the high-level tests would only catch slowly.
5. **`patch_replace` funnel:** `patch_replace` on an existing file with
   `new_string` containing a surrogateescape char → clean error, file
   byte-for-byte unchanged (validates the funnel path from the issue's
   motivating scenario).
6. **No-regression:** normal content round-trips byte-identical with
   `verified is True`.

Run via `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -q`.

## Out of scope

- Heredoc/payload transport fixes for remote backends — out of scope; the
  round-trip guarantee in this spec covers the POSIX pipe backends (local,
  ssh, docker, singularity) only.
- The Windows decode branch of `process_registry.write_stdin` (bytes → str for
  pywinpty); unrelated to this failure.
- `bytes_written` semantics in `process_registry` (character count vs byte
  count); unchanged, pre-existing behavior.
