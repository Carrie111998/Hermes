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
- Restructure `_write()`:

  ```python
  def _write():
      target = None
      try:
          raw = data.encode("utf-8", "surrogateescape") if isinstance(data, str) else data
          target = getattr(proc.stdin, "buffer", proc.stdin)
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
              if target is not None:
                  target.close()
          except Exception:
              pass
  ```

- The `errors` list is attached to the proc (`proc._hermes_stdin_errors = errors`)
  **before** the thread starts, so there is no race between the writer thread
  and `_wait_for_process`. Attaching `_hermes_*` attributes to Popen objects has
  precedent in this codebase (`proc._hermes_pgid` in `local.py`).
- `finally` guarantees stdin is always closed: a failed encode becomes EOF for
  the child instead of an infinite wait. `BrokenPipeError`/`OSError` keep their
  current pass semantics.

### 2. Propagation — `_wait_for_process` + `_exec`

- In `_wait_for_process` (`tools/environments/base.py:1210`), after the poll
  loop: if `proc._hermes_stdin_errors` is non-empty, append
  `[stdin write failed: <exc>]` to the rendered output and add a `"stdin_error"`
  key to the result dict. The child's real `returncode` is preserved.
- In `ShellFileOperations._exec` (`tools/file_operations.py:872`): a
  `stdin_error` on an otherwise-zero returncode maps to a failure
  (`exit_code = 1`), so `_atomic_write`'s existing `exit_code != 0` check
  surfaces a clear error instead of false persistence success. This is
  defense-in-depth — the write path is already protected by the pre-validation
  below — and it covers non-write stdin callers (`terminal_tool` background
  input) which read the message directly from the result output.

### 3. Write path — `write_file` (`tools/file_operations.py:1412`)

- Immediately before `_atomic_write` (after the BOM-prepend so a restored BOM
  is included in the hash), encode once:

  ```python
  try:
      content_bytes = content.encode("utf-8", "surrogateescape")
  except UnicodeEncodeError as exc:
      return WriteResult(error=(
          f"Refusing to write '{path}': content contains a lone surrogate "
          f"character ({exc}) that cannot be encoded as UTF-8. The file was "
          "NOT created or modified."
      ))
  ```

  Synchronous, deterministic, no child spawned, no hang, no empty-file
  truncation (a failed pipe write followed by EOF would otherwise let
  `cat > tmp` create an *empty* file and `mv` it over the target).
- Delete the `surrogatepass` encode at line 1598; use the pre-computed
  `content_bytes` for both `bytes_written` and the SHA-256. One encode serves
  validation + hash. Rewrite the now-incorrect comment block (1590–1597) to
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

---

## Edge cases

- **Windows:** the `proc.stdin.buffer` newline-translation workaround is
  untouched; `surrogateescape` is a codec and platform-independent.
- **Heredoc-mode remote backends** (modal, docker): untouched. Their stdin
  rides argv, which POSIX encodes with `surrogateescape`, so round-trip already
  works there; the hash fix in section 3 makes the verification agree with the
  bytes they actually receive.
- **Normal content:** `surrogateescape` is byte-identical to strict UTF-8 for
  any string without surrogates — zero behavior change for the common path,
  including the fail-closed syntax gate (runs before validation, unchanged).
- **Bytes input to `_pipe_stdin`:** the existing `isinstance(data, str)` branch
  is preserved; bytes pass through untouched.
- **Empty content / BOM-only content:** `b""` encodes fine; BOM prepended
  before validation is included in the hash exactly as it is written.

## Testing

New `tests/tools/test_file_write_surrogate_roundtrip.py`, using the real-env
fixture pattern from `tests/tools/test_file_tools_live.py` (real
`LocalEnvironment` + `ShellFileOperations`, temp cwd, no mocks):

1. **Round-trip:** `b"\xff\x00\xfe"` surrogateescape-decoded → `write_file` →
   on-disk bytes identical, `bytes_written == 3`, `verified is True` (the hash
   match that is broken today), no `.hermes-tmp*` leftovers, completes without
   hanging.
2. **Rejection:** `"\ud800"`, `"\udc7f"`, `"\udd00"` → `WriteResult.error`
   mentions "surrogate", file absent, error does **not** contain "timed out"
   (regression guard for the old hang).
3. **Propagation:** `env.execute("cat > /dev/null", stdin_data="\ud800")` →
   result carries `stdin_error`, `cat` exits 0 (proves stdin closed in
   `finally`, no hang).
4. **No-regression:** normal content round-trips byte-identical with
   `verified is True`.

Run via `scripts/run_tests.sh tests/tools/test_file_write_surrogate_roundtrip.py -q`.

## Out of scope

- Heredoc/payload transport fixes for remote backends (already correct via
  argv; verified by existing suites).
- The Windows decode branch of `process_registry.write_stdin` (bytes → str for
  pywinpty); unrelated to this failure.
- `bytes_written` semantics in `process_registry` (character count vs byte
  count); unchanged, pre-existing behavior.
