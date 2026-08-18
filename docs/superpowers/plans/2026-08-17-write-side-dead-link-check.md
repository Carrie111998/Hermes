# Write-side dead-link check — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Warn — never block — when a memory write introduces a `[[wikilink]]` to a name that was **deleted** from a memory root's git history, without firing on the sanctioned pattern of linking to a name not yet written.

**Architecture:** A tombstone check folded into the already-registered `PostToolUse` hook `memory-index-size-guard.py`, gated behind a default-off env flag. Only newly written text is scanned; targets are intersected with a cached set of names deleted from `~/.claude` git history. Tombstone-first means the hot path is a dict lookup — the cross-root index and its budget are never built.

**Tech Stack:** Python 3.11 stdlib only. Hooks run under `C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe`. Tests run under `C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest`.

**Spec:** `docs/superpowers/specs/2026-08-17-write-side-dead-link-check-design.md`

## Global Constraints

- **Stdlib only.** The hook interpreter has no third-party packages. Never `import pytest`/`requests`/anything third-party in hook code.
- **Never run bare `python`.** Tests: `C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest`. Hook smoke-runs: the uv interpreter above.
- **Exit 0 always. Never block.** Every path in this hook returns 0. No `permissionDecision`, no non-zero exit.
- **Default off.** `CLAUDE_MEMORY_TOMBSTONE_WARN` unset ⇒ behaviour byte-identical to today.
- **No `settings.json` change. No file rename.** Renaming this hook would leave the existing registration pointing at a missing file and silently kill the size guard.
- **`memory-link-lint.ps1` is READ-ONLY** (Diego, 2026-08-10). Never edit it.
- **`~/.claude` commit rules:** stage explicit paths, verify the staged set, then commit **bare**. Never `git add -A`, never `git commit -- <paths>`, never `--no-verify`.
- **Sibling sessions write this repo continuously.** `memory-index-size-guard.py` was modified by another session at `2026-08-18T02:06:11Z` (commit `8bbacff`) mid-planning. Re-read any file before editing it and check `git status` before staging.
- **Do not repair the two existing dead links.** They live in the `hermes-agent-src` root, owned by other sessions; Diego accepted them on 2026-08-17. This build stops the *next* one.

---

## Current state of the file being modified

`~/.claude/hooks/memory-index-size-guard.py` is **150 lines** as of commit `8bbacff`. Relevant existing members, which the tasks below consume and must not break:

- `HOME`, `LOG`, `DEFAULT_THRESHOLD = 170`, `HARNESS_CAP = 200`, `WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}`
- `_log(line)`, `_threshold()`, `_is_auto_memory_index(file_path)`, `_count_lines(file_path)`
- `main()` — parses stdin, gates on `WRITE_TOOLS` and `_is_auto_memory_index`, builds `msg`, and emits **both channels**: `{"systemMessage": msg, "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": msg}}`

Its five existing tests in `test_memory_index_size_guard_message.py` all pass (verified 2026-08-18T02:14Z). They assert substrings of `systemMessage` **and** of `hookSpecificOutput.additionalContext`, so any restructuring must preserve both channels.

## File structure

| File | Responsibility |
|---|---|
| `~/.claude/hooks/memory-index-size-guard.py` | **Modify.** Adds the tombstone check as functions independent of the size check; `main()` becomes a two-check dispatcher with a merged emit. |
| `~/.claude/hooks/test_memory_index_size_guard_message.py` | **Modify.** Adds the tombstone behavioural matrix beside the existing five message tests. |
| `~/.claude/hooks/memory_links.py` | **Read only — do not modify.** Supplies `iter_links`, `normalize`, `identities_from_text`, `derive_roots`, `root_label`, `_GIT_ENV_HIJACKERS`. |
| `~/.claude/logs/memory-tombstones.json` | **Runtime artifact.** Not committed; `~/.claude/logs/` is already untracked. |

**Deliberate divergence from the sibling hooks:** `block-memory-file-orphan.py` and `detect-memory-file-orphan.py` both do `ml = _load_memory_links()` at module scope, which raises on failure. This hook must **not** — a broken `memory_links.py` would then take the size guard down with it. Load it lazily inside the tombstone path, inside a `try`.

---

### Task 1: Detection primitives

Pure functions, no I/O, no git. A reviewer can accept these independently of any caching or wiring.

**Files:**
- Modify: `C:\Users\diego\.claude\hooks\memory-index-size-guard.py`
- Test: `C:\Users\diego\.claude\hooks\test_memory_index_size_guard_message.py`

**Interfaces:**
- Consumes: nothing from other tasks.
- Produces: `_tombstone_enabled() -> bool`, `_is_memory_file(file_path: str) -> bool`, `_new_text(tool_name: str, tool_input: dict) -> str`, and the constants `TOMBSTONE_FLAG`, `TOMBSTONE_CACHE`, `TOMBSTONE_MAX_AGE_SEC`, `CLAUDE_REPO`.

- [ ] **Step 1: Write the failing tests**

Append to `test_memory_index_size_guard_message.py`:

```python
# --- Task 1: detection primitives -------------------------------------------


def test_flag_is_off_by_default(monkeypatch):
    monkeypatch.delenv(hook.TOMBSTONE_FLAG, raising=False)
    assert hook._tombstone_enabled() is False


def test_flag_off_for_explicit_falsy_values(monkeypatch):
    for raw in ("", "  ", "0", "false", "False", "no", "off"):
        monkeypatch.setenv(hook.TOMBSTONE_FLAG, raw)
        assert hook._tombstone_enabled() is False, raw


def test_unrecognized_flag_values_do_not_enable(monkeypatch):
    """A default-off safety flag must not be switched ON by a typo. Pins the
    strict allowlist against a future 'anything not falsy' rewrite."""
    for raw in ("disabled", "banana", "2", "-1", "on ish"):
        monkeypatch.setenv(hook.TOMBSTONE_FLAG, raw)
        assert hook._tombstone_enabled() is False, raw


def test_flag_on_for_truthy_values(monkeypatch):
    for raw in ("1", "true", "yes", "on"):
        monkeypatch.setenv(hook.TOMBSTONE_FLAG, raw)
        assert hook._tombstone_enabled() is True, raw


def test_is_memory_file_accepts_any_md_in_a_memory_root():
    assert hook._is_memory_file(
        "C:/Users/diego/.claude/projects/C--Users-diego/memory/feedback_x.md"
    )
    assert hook._is_memory_file(
        r"C:\Users\diego\.claude\projects\C--Users-diego--hermes\memory\MEMORY.md"
    )


def test_is_memory_file_rejects_non_memory_paths():
    """The predicate is broader than _is_auto_memory_index but must not be
    unbounded: a docs page or a hook source is not a memory file."""
    for p in (
        "",
        "C:/Users/diego/.claude/hooks/memory_links.py",
        "C:/Users/diego/.claude/projects/C--Users-diego/memory/notes.txt",
        "C:/Users/diego/some/project/memory/thing.md",
        "C:/Users/diego/.claude/projects/C--Users-diego/memory/sub/deep.md",
    ):
        assert hook._is_memory_file(p) is False, p


def test_new_text_extracts_per_tool():
    assert hook._new_text("Write", {"content": "hello [[a]]"}) == "hello [[a]]"
    assert hook._new_text("Edit", {"new_string": "hello [[b]]"}) == "hello [[b]]"
    assert hook._new_text(
        "MultiEdit",
        {"edits": [{"new_string": "one [[c]]"}, {"new_string": "two [[d]]"}]},
    ) == "one [[c]]\ntwo [[d]]"


def test_new_text_ignores_old_string_and_malformed_edits():
    """Only NEWLY WRITTEN text is scanned -- a link being REMOVED must never
    warn, and a malformed edits list must not raise."""
    assert hook._new_text("Edit", {"old_string": "[[gone]]", "new_string": ""}) == ""
    assert hook._new_text("MultiEdit", {"edits": ["not-a-dict", {"x": 1}]}) == ""
    assert hook._new_text("Read", {"content": "[[e]]"}) == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: FAIL — `AttributeError: module 'memory_index_size_guard' has no attribute 'TOMBSTONE_FLAG'`.

- [ ] **Step 3: Write the implementation**

In `memory-index-size-guard.py`, add `import re` to the imports (keep them alphabetical: `datetime as _dt`, `json`, `os`, `re`, `sys`, `traceback`), then add after the `WRITE_TOOLS` constant:

```python
# --- write-side dead-link (tombstone) check ---------------------------------
# Gates on a name having been DELETED from git history, not on whether it
# resolves. A link to a name that never existed is sanctioned usage ("link
# liberally -- a [[name]] that doesn't match an existing memory yet is fine").
# Measured 2026-08-17: resolution-checking fires on 54 links of which 52 are
# legitimate; tombstone-filtering the same 54 leaves exactly the 2 real cases.
TOMBSTONE_FLAG = "CLAUDE_MEMORY_TOMBSTONE_WARN"
TOMBSTONE_CACHE = os.path.join(HOME, ".claude", "logs", "memory-tombstones.json")
TOMBSTONE_MAX_AGE_SEC = 24 * 3600
CLAUDE_REPO = os.path.join(HOME, ".claude")

# Strict allowlist, NOT "anything that isn't falsy". A default-off safety flag
# must not be switched ON by a typo: CLAUDE_MEMORY_TOMBSTONE_WARN=disabled has
# to mean disabled.
_TRUTHY = {"1", "true", "yes", "on"}

# A memory file is a *.md directly inside a memory root -- no subdirectories,
# since the roots are flat and a nested path is not a linkable memory.
_MEMORY_PATH_RE = re.compile(r"/\.claude/projects/[^/]+/memory/[^/]+\.md$")


def _tombstone_enabled() -> bool:
    return (os.environ.get(TOMBSTONE_FLAG) or "").strip().lower() in _TRUTHY


def _is_memory_file(file_path: str) -> bool:
    """Any linkable memory file, not just the index that _is_auto_memory_index
    matches. Both predicates can be true for the same write; the two checks are
    independent."""
    if not file_path:
        return False
    return bool(_MEMORY_PATH_RE.search(file_path.replace("\\", "/").lower()))


def _new_text(tool_name: str, tool_input: dict) -> str:
    """Only the text this write ADDS. Scanning the file on disk instead would
    re-warn about pre-existing links every time the file was touched."""
    if tool_name == "Write":
        return tool_input.get("content") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string") or ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        return "\n".join(
            (e.get("new_string") or "") for e in edits if isinstance(e, dict)
        )
    return ""
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: PASS — 13 passed (5 pre-existing + 8 new).

- [ ] **Step 5: Verify only your paths are staged, then commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/memory-index-size-guard.py hooks/test_memory_index_size_guard_message.py
```

```bash
git -C "C:/Users/diego/.claude" diff --cached --name-only
```

Expected output — exactly these two lines and nothing else:

```
hooks/memory-index-size-guard.py
hooks/test_memory_index_size_guard_message.py
```

If any other path appears, a sibling session staged it: unstage with `git -C "C:/Users/diego/.claude" restore --staged -- <that path>` and re-check before committing.

```bash
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): tombstone-check detection primitives"
```

---

### Task 2: Tombstone set and its cache

**Files:**
- Modify: `C:\Users\diego\.claude\hooks\memory-index-size-guard.py`
- Test: `C:\Users\diego\.claude\hooks\test_memory_index_size_guard_message.py`

**Interfaces:**
- Consumes: `TOMBSTONE_CACHE`, `TOMBSTONE_MAX_AGE_SEC`, `CLAUDE_REPO`, `_log` (Task 1 / existing).
- Produces: `_load_memory_links() -> module`, `_git_tombstones(repo: str, timeout: float = 20.0) -> dict[str, dict]`, `_load_tombstones(cache_path: str, repo: str, max_age: float = TOMBSTONE_MAX_AGE_SEC, now: float | None = None) -> dict[str, dict]`. The returned mapping is `{normalized_name: {"commit": str, "date": str}}`.

- [ ] **Step 1: Write the failing tests**

Append to `test_memory_index_size_guard_message.py`:

```python
# --- Task 2: tombstone cache ------------------------------------------------

import time as _time


def _write_cache(path, names, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"built": 0, "names": names}), encoding="utf-8"
    )
    if mtime is not None:
        import os as _os
        _os.utime(path, (mtime, mtime))
    return path


def test_fresh_cache_is_used_without_calling_git(tmp_path, monkeypatch):
    cache = _write_cache(
        tmp_path / "tomb.json",
        {"gone_name": {"commit": "486258b", "date": "2026-08-14"}},
        mtime=_time.time(),
    )

    def _explode(*a, **k):
        raise AssertionError("git must not be called for a fresh cache")

    monkeypatch.setattr(hook, "_git_tombstones", _explode)
    got = hook._load_tombstones(str(cache), "irrelevant", now=_time.time())
    assert got == {"gone_name": {"commit": "486258b", "date": "2026-08-14"}}


def test_stale_cache_triggers_a_rebuild(tmp_path, monkeypatch):
    cache = _write_cache(
        tmp_path / "tomb.json", {"old": {"commit": "a", "date": "d"}}, mtime=0
    )
    monkeypatch.setattr(
        hook, "_git_tombstones", lambda repo, **k: {"new": {"commit": "b", "date": "e"}}
    )
    got = hook._load_tombstones(str(cache), "repo", now=_time.time())
    assert got == {"new": {"commit": "b", "date": "e"}}
    assert json.loads(cache.read_text(encoding="utf-8"))["names"] == got


def test_stale_cache_is_still_used_when_the_rebuild_fails(tmp_path, monkeypatch):
    """Deletions are append-only in git history, so a stale set under-reports
    and can never over-report. The failure direction must be a MISSED warning,
    never a false one -- so keep serving the stale set."""
    cache = _write_cache(
        tmp_path / "tomb.json", {"old": {"commit": "a", "date": "d"}}, mtime=0
    )

    def _boom(repo, **k):
        raise RuntimeError("git exploded")

    monkeypatch.setattr(hook, "_git_tombstones", _boom)
    monkeypatch.setattr(hook, "LOG", str(tmp_path / "log.txt"))
    got = hook._load_tombstones(str(cache), "repo", now=_time.time())
    assert got == {"old": {"commit": "a", "date": "d"}}


def test_missing_cache_and_failing_git_yields_empty_not_raise(tmp_path, monkeypatch):
    def _boom(repo, **k):
        raise RuntimeError("no git")

    monkeypatch.setattr(hook, "_git_tombstones", _boom)
    monkeypatch.setattr(hook, "LOG", str(tmp_path / "log.txt"))
    assert hook._load_tombstones(str(tmp_path / "absent.json"), "repo") == {}


def test_malformed_cache_is_treated_as_missing(tmp_path, monkeypatch):
    bad = tmp_path / "tomb.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(
        hook, "_git_tombstones", lambda repo, **k: {"n": {"commit": "c", "date": "d"}}
    )
    monkeypatch.setattr(hook, "LOG", str(tmp_path / "log.txt"))
    assert hook._load_tombstones(str(bad), "repo", now=_time.time()) == {
        "n": {"commit": "c", "date": "d"}
    }


def test_git_tombstones_parses_commits_and_names(tmp_path, monkeypatch):
    """Parses the NUL-prefixed header lines emitted by --pretty=format, keeping
    the MOST RECENT deletion for a name that was deleted more than once."""
    stdout = (
        "\x00abc1234 2026-08-14T21:47:09-04:00\n"
        "\n"
        "projects/C--Users-diego--hermes-agent-src/memory/detached-launch-can-silently-never-start.md\n"
        "projects/C--Users-diego--hermes-agent-src/memory/other-thing.md\n"
        "\x00def5678 2026-07-10T10:30:14-04:00\n"
        "\n"
        "projects/C--Users-diego--hermes/memory/other-thing.md\n"
    )

    class _Proc:
        returncode = 0

    proc = _Proc()
    proc.stdout = stdout
    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: proc)

    got = hook._git_tombstones("repo")
    assert got["detached_launch_can_silently_never_start"] == {
        "commit": "abc1234",
        "date": "2026-08-14",
    }
    assert got["other_thing"]["commit"] == "abc1234", "newest deletion must win"


def test_git_tombstones_raises_on_nonzero_rc(monkeypatch):
    class _Proc:
        returncode = 128
        stdout = ""

    monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: _Proc())
    try:
        hook._git_tombstones("repo")
    except RuntimeError:
        return
    raise AssertionError("expected RuntimeError on non-zero git rc")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: FAIL — `AttributeError: module 'memory_index_size_guard' has no attribute '_load_tombstones'`.

- [ ] **Step 3: Write the implementation**

Add `import importlib.util`, `import subprocess`, `import time` and `from pathlib import Path` to the imports, then append after `_new_text`:

```python
def _load_memory_links():
    """Loaded LAZILY and inside a try by callers -- deliberately unlike the
    sibling orphan hooks, which bind it at module scope. A broken
    memory_links.py must not take the index-size guard down with it."""
    path = Path(__file__).resolve().parent / "memory_links.py"
    spec = importlib.util.spec_from_file_location("memory_links", path)
    if not spec or not spec.loader:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["memory_links"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git_tombstones(repo: str, timeout: float = 20.0) -> dict:
    """Every memory name ever deleted, with the commit that destroyed it.

    The NUL prefix on the pretty format makes commit headers unambiguous
    against filenames -- a filename can contain spaces and dates, a header
    cannot contain NUL.

    Git env vars are cleared for the child: they hijack even an explicit
    `git -C`, and this box runs ~20 worktrees with concurrent agents.
    """
    ml = _load_memory_links()
    env = {
        k: v for k, v in os.environ.items() if k not in ml._GIT_ENV_HIJACKERS
    }
    proc = subprocess.run(
        [
            "git", "-C", repo, "log", "--diff-filter=D", "--name-only",
            "--pretty=format:%x00%h %cI", "--", "projects/*/memory/*.md",
        ],
        capture_output=True, text=True, timeout=timeout, env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git log failed rc={proc.returncode}")

    out: dict = {}
    commit = ""
    date = ""
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if line.startswith("\x00"):
            head = line[1:].split(" ", 1)
            commit = head[0]
            date = head[1][:10] if len(head) > 1 else ""
            continue
        if line.endswith(".md"):
            # git log is newest-first, so setdefault keeps the MOST RECENT
            # deletion of a name deleted more than once.
            out.setdefault(
                ml.normalize(Path(line).stem), {"commit": commit, "date": date}
            )
    return out


def _load_tombstones(
    cache_path: str,
    repo: str,
    max_age: float = TOMBSTONE_MAX_AGE_SEC,
    now: float | None = None,
) -> dict:
    """Cached tombstone set. Fails open to {} -- never raises."""
    now = time.time() if now is None else now

    cached = None
    try:
        mtime = os.stat(cache_path).st_mtime
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("names"), dict):
            cached = data["names"]
            if now - mtime < max_age:
                return cached
    except Exception:
        cached = None

    try:
        names = _git_tombstones(repo)
    except Exception as e:
        _log(f"tombstone-refresh-failed {e}")
        return cached if cached is not None else {}

    try:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        tmp = cache_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"built": now, "names": names}, f)
        # Atomic: concurrent sessions may refresh this cache simultaneously.
        os.replace(tmp, cache_path)
    except Exception as e:
        _log(f"tombstone-cache-write-failed {e}")
    return names
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: PASS — 20 passed.

- [ ] **Step 5: Verify the real git path works against the live repo**

```bash
"C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe" -c "import importlib.util,pathlib; p=pathlib.Path(r'C:/Users/diego/.claude/hooks/memory-index-size-guard.py'); s=importlib.util.spec_from_file_location('h',p); m=importlib.util.module_from_spec(s); s.loader.exec_module(m); t=m._git_tombstones(m.CLAUDE_REPO); print(len(t), t.get('detached_launch_can_silently_never_start'))"
```

Expected: `136 {'commit': '486258b', 'date': '2026-08-14'}` — the count may be higher if a sibling has since deleted more memory files, but it must be ≥ 136 and the `486258b` entry must be present.

- [ ] **Step 6: Verify only your paths are staged, then commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/memory-index-size-guard.py hooks/test_memory_index_size_guard_message.py
```

```bash
git -C "C:/Users/diego/.claude" diff --cached --name-only
```

Expected — exactly these two lines and nothing else:

```
hooks/memory-index-size-guard.py
hooks/test_memory_index_size_guard_message.py
```

If any other path appears, a sibling staged it: `git -C "C:/Users/diego/.claude" restore --staged -- <that path>`, then re-check.

```bash
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): git tombstone set with lazily-rebuilt cache"
```

---

### Task 3: The check and its message

**Files:**
- Modify: `C:\Users\diego\.claude\hooks\memory-index-size-guard.py`
- Test: `C:\Users\diego\.claude\hooks\test_memory_index_size_guard_message.py`

**Interfaces:**
- Consumes: `_load_memory_links`, `_load_tombstones` (Task 2), `_new_text` (Task 1).
- Produces: `_live_identities(roots) -> set[str]`, `_tombstone_hits(text: str, tombstones: dict, roots: list) -> list[tuple[str, str, dict]]` returning `(original_target, normalized_key, meta)`, and `_tombstone_message(hits) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `test_memory_index_size_guard_message.py`:

```python
# --- Task 3: the check ------------------------------------------------------

TOMB = {
    "read_test_durations_json_before_reproducing_a_gate_flake": {
        "commit": "486258b",
        "date": "2026-08-14",
    },
    "resurrected_name": {"commit": "aaa1111", "date": "2026-08-01"},
}


def _root_with(tmp_path, *names):
    root = tmp_path / ".claude" / "projects" / "C--Users-diego" / "memory"
    root.mkdir(parents=True, exist_ok=True)
    for n in names:
        (root / f"{n}.md").write_text("body\n", encoding="utf-8")
    return [root]


def test_link_to_a_tombstoned_name_is_a_hit(tmp_path):
    roots = _root_with(tmp_path, "unrelated")
    hits = hook._tombstone_hits(
        "see [[read-test-durations-json-before-reproducing-a-gate-flake]]",
        TOMB,
        roots,
    )
    assert len(hits) == 1
    target, key, meta = hits[0]
    assert key == "read_test_durations_json_before_reproducing_a_gate_flake"
    assert meta["commit"] == "486258b"


def test_aspirational_link_to_a_never_existing_name_is_silent(tmp_path):
    """THE test that matters most. The harness instructs agents to link
    liberally to names not yet written; a check that fires on that is worse
    than no check, because it will be switched off."""
    roots = _root_with(tmp_path, "unrelated")
    assert hook._tombstone_hits("planning [[some-future-memory]]", TOMB, roots) == []


def test_link_to_a_live_memory_is_silent(tmp_path):
    roots = _root_with(tmp_path, "an-existing-note")
    assert hook._tombstone_hits("see [[an-existing-note]]", TOMB, roots) == []


def test_resurrected_name_is_silent(tmp_path):
    """Tombstoned but re-created. Zero such names exist today; this keeps the
    check correct on the day one does."""
    roots = _root_with(tmp_path, "resurrected-name")
    assert hook._tombstone_hits("see [[resurrected-name]]", TOMB, roots) == []


def test_frontmatter_name_counts_as_live(tmp_path):
    """A memory's identity is its filename AND its frontmatter slug."""
    roots = _root_with(tmp_path)
    (roots[0] / "different-filename.md").write_text(
        "---\nname: resurrected-name\n---\nbody\n", encoding="utf-8"
    )
    assert hook._tombstone_hits("see [[resurrected-name]]", TOMB, roots) == []


def test_link_inside_a_code_fence_is_not_a_link(tmp_path):
    roots = _root_with(tmp_path, "unrelated")
    text = (
        "```bash\n"
        "echo [[read-test-durations-json-before-reproducing-a-gate-flake]]\n"
        "```\n"
    )
    assert hook._tombstone_hits(text, TOMB, roots) == []


def test_duplicate_links_report_once(tmp_path):
    roots = _root_with(tmp_path, "unrelated")
    text = (
        "[[read-test-durations-json-before-reproducing-a-gate-flake]] and again "
        "[[read-test-durations-json-before-reproducing-a-gate-flake]]"
    )
    assert len(hook._tombstone_hits(text, TOMB, roots)) == 1


def test_message_names_the_target_the_commit_and_the_date():
    hits = [
        (
            "read-test-durations-json-before-reproducing-a-gate-flake",
            "read_test_durations_json_before_reproducing_a_gate_flake",
            {"commit": "486258b", "date": "2026-08-14"},
        )
    ]
    msg = hook._tombstone_message(hits)
    assert "[[read-test-durations-json-before-reproducing-a-gate-flake]]" in msg
    assert "486258b" in msg
    assert "2026-08-14" in msg


def test_message_distinguishes_dead_from_aspirational():
    """The agent must be told WHY this link differs from the sanctioned
    link-liberally pattern, or it will read the warning as a false positive."""
    hits = [("x", "x", {"commit": "c", "date": "d"})]
    msg = hook._tombstone_message(hits)
    assert "not an aspirational link" in msg.lower()
    assert "inline" in msg.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: FAIL — `AttributeError: module 'memory_index_size_guard' has no attribute '_tombstone_hits'`.

- [ ] **Step 3: Write the implementation**

Append after `_load_tombstones`:

```python
def _live_identities(roots) -> set:
    """Every name currently resolvable, filename and frontmatter slug alike.

    Called ONLY when a candidate hit exists -- ~0 times per day -- so its
    ~0.8s cost never lands on the hot write path.
    """
    ml = _load_memory_links()
    live: set = set()
    for root in roots:
        try:
            entries = sorted(root.glob("*.md"))
        except OSError:
            continue
        for path in entries:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            live |= ml.identities_from_text(path.stem, text)
    return live


def _tombstone_hits(text: str, tombstones: dict, roots) -> list:
    """Links in `text` whose target was deleted and is still gone.

    Returns [(original_target, normalized_key, meta), ...], one entry per
    distinct target.
    """
    if not text or not tombstones:
        return []
    ml = _load_memory_links()
    candidates: dict = {}
    for _lineno, target, _kind in ml.iter_links(text.splitlines()):
        key = ml.normalize(target)
        if key in tombstones and key not in candidates:
            candidates[key] = target
    if not candidates:
        return []
    live = _live_identities(roots)
    return [
        (target, key, tombstones[key])
        for key, target in sorted(candidates.items())
        if key not in live
    ]


def _tombstone_message(hits) -> str:
    listed = "; ".join(
        f"[[{target}]] (destroyed by {meta.get('commit') or 'an earlier commit'}"
        f" on {meta.get('date') or 'an unknown date'})"
        for target, _key, meta in hits
    )
    plural = "s" if len(hits) != 1 else ""
    verb = "" if len(hits) != 1 else "s"
    return (
        f"DEAD LINK WRITTEN: {len(hits)} link{plural} in this write "
        f"point{verb} at a memory that was DELETED: {listed}. "
        f"This is NOT an aspirational link to a memory you intend to write "
        f"later -- the target existed and is gone, so nothing will ever "
        f"resolve it. Repoint it at the surviving memory that absorbed the "
        f"fact, or inline the fact here. If the survivor lives in ANOTHER "
        f"memory root, inline it -- repointing only converts a dead link into "
        f"a cross-root one."
    )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: PASS — 29 passed.

- [ ] **Step 5: Verify only your paths are staged, then commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/memory-index-size-guard.py hooks/test_memory_index_size_guard_message.py
```

```bash
git -C "C:/Users/diego/.claude" diff --cached --name-only
```

Expected — exactly these two lines and nothing else:

```
hooks/memory-index-size-guard.py
hooks/test_memory_index_size_guard_message.py
```

```bash
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): tombstone hit detection and warning message"
```

---

### Task 4: Wire into `main()` with a merged dual-channel emit

The size check becomes a function so `main()` can run both checks independently. This is the only task that touches existing behaviour, so it is the one a reviewer should scrutinise hardest.

**Files:**
- Modify: `C:\Users\diego\.claude\hooks\memory-index-size-guard.py:87-149`
- Test: `C:\Users\diego\.claude\hooks\test_memory_index_size_guard_message.py`

**Interfaces:**
- Consumes: everything from Tasks 1–3.
- Produces: `_size_message(payload: dict) -> str | None`, `_tombstone_check(payload: dict) -> str | None`, and a `main()` that emits `{"systemMessage": ..., "hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": ...}}` carrying both messages joined by a blank line.

- [ ] **Step 1: Write the failing tests**

Append to `test_memory_index_size_guard_message.py`:

```python
# --- Task 4: main() integration ---------------------------------------------


def _run(tmp_path, monkeypatch, capsys, payload):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(hook, "LOG", str(tmp_path / "log.txt"))
    assert hook.main() == 0
    out = capsys.readouterr().out.strip()
    return json.loads(out) if out else None


def _memory_file(tmp_path, name="feedback_thing.md", body="body\n"):
    p = tmp_path / ".claude" / "projects" / "C--Users-diego" / "memory" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


def test_flag_off_emits_nothing_for_a_dead_link(tmp_path, monkeypatch, capsys):
    """Landing inert is the whole rollout plan. With the flag unset the hook
    must behave byte-identically to before this build."""
    monkeypatch.delenv(hook.TOMBSTONE_FLAG, raising=False)
    monkeypatch.setattr(hook, "_load_tombstones", lambda *a, **k: TOMB)
    p = _memory_file(tmp_path)
    assert _run(tmp_path, monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(p),
            "content": "[[read-test-durations-json-before-reproducing-a-gate-flake]]",
        },
    }) is None


def test_flag_on_warns_about_a_dead_link(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(hook.TOMBSTONE_FLAG, "1")
    monkeypatch.setattr(hook, "_load_tombstones", lambda *a, **k: TOMB)
    p = _memory_file(tmp_path)
    monkeypatch.setattr(hook, "_memory_roots", lambda: [p.parent])
    emitted = _run(tmp_path, monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(p),
            "content": "[[read-test-durations-json-before-reproducing-a-gate-flake]]",
        },
    })
    assert "DEAD LINK WRITTEN" in emitted["systemMessage"]
    assert "486258b" in emitted["systemMessage"]
    assert "DEAD LINK WRITTEN" in emitted["hookSpecificOutput"]["additionalContext"]
    assert emitted["hookSpecificOutput"]["hookEventName"] == "PostToolUse"


def test_flag_on_is_silent_for_a_non_memory_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(hook.TOMBSTONE_FLAG, "1")
    monkeypatch.setattr(hook, "_load_tombstones", lambda *a, **k: TOMB)
    src = tmp_path / "src.py"
    src.write_text("x\n", encoding="utf-8")
    assert _run(tmp_path, monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(src),
            "content": "[[read-test-durations-json-before-reproducing-a-gate-flake]]",
        },
    }) is None


def test_multiedit_second_edit_is_scanned(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(hook.TOMBSTONE_FLAG, "1")
    monkeypatch.setattr(hook, "_load_tombstones", lambda *a, **k: TOMB)
    p = _memory_file(tmp_path)
    monkeypatch.setattr(hook, "_memory_roots", lambda: [p.parent])
    emitted = _run(tmp_path, monkeypatch, capsys, {
        "tool_name": "MultiEdit",
        "tool_input": {
            "file_path": str(p),
            "edits": [
                {"new_string": "harmless"},
                {"new_string": "[[read-test-durations-json-before-reproducing-a-gate-flake]]"},
            ],
        },
    })
    assert "DEAD LINK WRITTEN" in emitted["systemMessage"]


def test_a_preexisting_dead_link_outside_the_edit_is_silent(
    tmp_path, monkeypatch, capsys
):
    """Only NEWLY WRITTEN text is scanned. A file that already contains a dead
    link must not nag every time an unrelated part of it is edited -- that is
    the difference between a warning and an alarm nobody reads."""
    monkeypatch.setenv(hook.TOMBSTONE_FLAG, "1")
    monkeypatch.setattr(hook, "_load_tombstones", lambda *a, **k: TOMB)
    p = _memory_file(
        tmp_path,
        body="[[read-test-durations-json-before-reproducing-a-gate-flake]]\nrest\n",
    )
    monkeypatch.setattr(hook, "_memory_roots", lambda: [p.parent])
    assert _run(tmp_path, monkeypatch, capsys, {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(p), "new_string": "an unrelated change"},
    }) is None


def test_both_checks_fire_in_one_payload(tmp_path, monkeypatch, capsys):
    """A dead link written INTO the oversized index. One JSON object, both
    messages, both channels."""
    monkeypatch.setenv(hook.TOMBSTONE_FLAG, "1")
    monkeypatch.setattr(hook, "_load_tombstones", lambda *a, **k: TOMB)
    idx = _memory_file(tmp_path, "MEMORY.md", "x\n" * 500)
    monkeypatch.setattr(hook, "_memory_roots", lambda: [idx.parent])
    emitted = _run(tmp_path, monkeypatch, capsys, {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(idx),
            "new_string": "[[read-test-durations-json-before-reproducing-a-gate-flake]]",
        },
    })
    msg = emitted["systemMessage"]
    assert "consolidate-memory" in msg
    assert "DEAD LINK WRITTEN" in msg
    assert "consolidate-memory" in emitted["hookSpecificOutput"]["additionalContext"]
    assert "DEAD LINK WRITTEN" in emitted["hookSpecificOutput"]["additionalContext"]


def test_a_raising_tombstone_check_cannot_suppress_the_size_warning(
    tmp_path, monkeypatch, capsys
):
    """The two checks are independent. Neither may take the other down."""
    monkeypatch.setenv(hook.TOMBSTONE_FLAG, "1")

    def _boom(*a, **k):
        raise RuntimeError("tombstone path exploded")

    monkeypatch.setattr(hook, "_tombstone_check", _boom)
    idx = _memory_file(tmp_path, "MEMORY.md", "x\n" * 500)
    emitted = _run(tmp_path, monkeypatch, capsys, {
        "tool_name": "Edit", "tool_input": {"file_path": str(idx)},
    })
    assert "consolidate-memory" in emitted["systemMessage"]


def test_a_raising_size_check_cannot_suppress_the_dead_link_warning(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv(hook.TOMBSTONE_FLAG, "1")
    monkeypatch.setattr(hook, "_load_tombstones", lambda *a, **k: TOMB)

    def _boom(*a, **k):
        raise RuntimeError("size path exploded")

    monkeypatch.setattr(hook, "_size_message", _boom)
    p = _memory_file(tmp_path)
    monkeypatch.setattr(hook, "_memory_roots", lambda: [p.parent])
    emitted = _run(tmp_path, monkeypatch, capsys, {
        "tool_name": "Write",
        "tool_input": {
            "file_path": str(p),
            "content": "[[read-test-durations-json-before-reproducing-a-gate-flake]]",
        },
    })
    assert "DEAD LINK WRITTEN" in emitted["systemMessage"]
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_memory_index_size_guard_message.py -q
```

Expected: FAIL — `AttributeError: module 'memory_index_size_guard' has no attribute '_memory_roots'`.

- [ ] **Step 3: Write the implementation**

Add `_memory_roots` after `_live_identities`:

```python
def _memory_roots() -> list:
    """The versioned memory roots. A seam the tests replace, so no test
    depends on the live corpus a sibling session may be writing to."""
    return _load_memory_links().derive_roots()
```

Then replace the body of `main()` from line 87 to the end of the file with:

```python
def _size_message(payload: dict):
    """The pre-existing index-size warning. Returns None when it does not fire."""
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not _is_auto_memory_index(file_path):
        return None

    threshold = _threshold()
    if threshold <= 0:
        return None

    lines = _count_lines(file_path)
    if lines < threshold:
        _log(f"ok {file_path} lines={lines} threshold={threshold}")
        return None

    _log(f"WARN {file_path} lines={lines} threshold={threshold}")
    return (
        f"MEMORY.md index is {lines} lines (early-warning threshold {threshold}; "
        f"harness auto-memory read cap ~{HARNESS_CAP}). Consider a consolidation "
        f"pass: invoke the /consolidate-memory skill to merge related entries and "
        f"relocate detail into topic files before the index gets truncated.\n"
        f"LINK SAFETY: before deleting a file whose content you merged, resolve "
        f"its inbound [[wikilinks]] and repoint them at the merge target. If a "
        f"referrer lives in ANOTHER memory root, inline the fact into that "
        f"referrer instead -- repointing only converts a dead link into a "
        f"cross-root one. Deletions that would orphan a link are blocked by the "
        f"PreToolUse guard; fix the referrers and re-issue the same command."
    )


def _tombstone_check(payload: dict):
    """The write-side dead-link warning. Returns None when it does not fire."""
    if not _tombstone_enabled():
        return None
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not _is_memory_file(file_path):
        return None
    text = _new_text(payload.get("tool_name") or "", tool_input)
    if not text:
        return None
    tombstones = _load_tombstones(TOMBSTONE_CACHE, CLAUDE_REPO)
    hits = _tombstone_hits(text, tombstones, _memory_roots())
    if not hits:
        return None
    _log(f"DEADLINK {file_path} targets={[h[1] for h in hits]}")
    return _tombstone_message(hits)


def main() -> int:
    try:
        raw = sys.stdin.read() or "{}"
        # Strip a UTF-8 BOM before parsing. Claude Code's own payload has none,
        # but a PowerShell pipe (`'{...}' | python hook.py`) prepends one, and
        # json.loads rejects it outright -- which aborted the hook so no
        # index-size warning was emitted. Same fix as auto-commit-claude-memory.py.
        payload = json.loads(raw.lstrip("\ufeff"))
    except Exception as e:
        _log(f"parse-error {e}")
        return 0

    if (payload.get("tool_name") or "") not in WRITE_TOOLS:
        return 0

    # The two checks are independent: neither may take the other down.
    messages = []
    for name, check in (("size", _size_message), ("tombstone", _tombstone_check)):
        try:
            got = check(payload)
        except Exception as e:
            _log(f"{name}-check-failed {e}\n{traceback.format_exc()}")
            continue
        if got:
            messages.append(got)

    if not messages:
        return 0

    msg = "\n\n".join(messages)
    try:
        # BOTH channels. The LINK SAFETY paragraph is an instruction to the
        # AGENT, but `systemMessage` is documented as a warning shown to the
        # USER -- on that channel alone this hook's deliverable never reaches
        # the thing it is addressed to. `hookSpecificOutput.additionalContext`
        # is the model-facing channel.
        print(json.dumps({
            "systemMessage": msg,
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": msg,
            },
        }))
    except Exception as e:
        _log(f"emit-failed {e}\n{traceback.format_exc()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note the BOM literal is written as `"\ufeff"` rather than a raw BOM character, so the source stays ASCII and cannot be mangled by an editor that rewrites encodings.

- [ ] **Step 4: Run the full hook suite to verify everything passes**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/ -q
```

Expected: PASS. All 5 original size-guard message tests still green, plus the new tests — 37 in this file — and every other hook suite unchanged.

- [ ] **Step 5: Verify the hook is inert end-to-end under the real interpreter**

```bash
echo '{"tool_name":"Write","tool_input":{"file_path":"C:/Users/diego/.claude/projects/C--Users-diego/memory/zz_probe.md","content":"[[read-test-durations-json-before-reproducing-a-gate-flake]]"}}' | "C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe" "C:/Users/diego/.claude/hooks/memory-index-size-guard.py"; echo "rc=$?"
```

Expected: no output at all, `rc=0`. The flag is unset, so the check is inert — this is the property the rollout depends on.

- [ ] **Step 6: Verify only your paths are staged, then commit**

```bash
git -C "C:/Users/diego/.claude" add -- hooks/memory-index-size-guard.py hooks/test_memory_index_size_guard_message.py
```

```bash
git -C "C:/Users/diego/.claude" diff --cached --name-only
```

Expected — exactly these two lines and nothing else:

```
hooks/memory-index-size-guard.py
hooks/test_memory_index_size_guard_message.py
```

```bash
git -C "C:/Users/diego/.claude" commit -m "feat(hooks): write-side dead-link check, default off"
```

---

### Task 5: Acceptance sweep and spec close-out

Proves the check fires on exactly the two known instances across the whole live corpus and nothing else — the claim the spec makes and the reason this design was chosen over resolution-checking.

**Files:**
- Modify: `C:\Users\diego\.hermes\agent-src\docs\superpowers\specs\2026-08-17-write-side-dead-link-check-design.md`

- [ ] **Step 1: Run the acceptance sweep against the live corpus**

```bash
"C:/Users/diego/AppData/Roaming/uv/python/cpython-3.11.9-windows-x86_64-none/python.exe" -c "import importlib.util,pathlib; p=pathlib.Path(r'C:/Users/diego/.claude/hooks/memory-index-size-guard.py'); s=importlib.util.spec_from_file_location('h',p); h=importlib.util.module_from_spec(s); s.loader.exec_module(h); ml=h._load_memory_links(); roots=ml.derive_roots(); t=h._load_tombstones(h.TOMBSTONE_CACHE,h.CLAUDE_REPO); n=0; hits=[]; [ (hits.extend((ml.root_label(r), f.name, x[0]) for x in h._tombstone_hits(f.read_text(encoding='utf-8',errors='replace'), t, roots))) for r in roots for f in sorted(r.glob('*.md')) ]; print(len(hits)); [print(' ', *x) for x in sorted(hits)]"
```

Expected: `2`, followed by exactly these two lines:

```
  hermes-agent-src cron-pytest-gates-flipped-interpreter-2026-08-15.md read-test-durations-json-before-reproducing-a-gate-flake
  hermes-agent-src tests-tools-windows-baseline.md detached-launch-can-silently-never-start
```

If a third appears, a sibling wrote a new dead link since 2026-08-17 — record it, do not repair it, and add it to `PREEXISTING_DEAD` in `test_linter_conformance.py` only if the linter agrees it is DEAD.

- [ ] **Step 2: Verify the linter's DEAD set is unchanged**

```bash
C:/Users/diego/.hermes/agent-src/.venv/Scripts/python.exe -m pytest C:/Users/diego/.claude/hooks/test_linter_conformance.py -q
```

Expected: PASS. This build writes no memory files, so the DEAD set must be exactly the two pinned entries.

- [ ] **Step 3: Update the spec status line**

In `docs/superpowers/specs/2026-08-17-write-side-dead-link-check-design.md`, change:

```markdown
Status: approved (Diego, 2026-08-17), pending implementation
```

to:

```markdown
Status: implemented 2026-08-18, landed INERT (CLAUDE_MEMORY_TOMBSTONE_WARN unset). Acceptance sweep: 2 hits, both PREEXISTING_DEAD. Awaiting Diego's review before activation.
```

- [ ] **Step 4: Commit the spec via the mandated wrapper**

`~/.hermes/agent-src` requires the wrapper — never a bare `git commit` there.

```bash
python C:\Users\diego\.hermes\ops\git-quiet-commit.py -C C:\Users\diego\.hermes\agent-src -m "docs: write-side dead-link check implemented, landed inert" -- docs/superpowers/specs/2026-08-17-write-side-dead-link-check-design.md
```

Expected: pre-commit hooks run (`Detect hardcoded secrets` passes), one commit created, nothing pushed.

- [ ] **Step 5: Report to Diego**

State: the acceptance result, that the code is inert, and the exact one-line change that activates it — set `CLAUDE_MEMORY_TOMBSTONE_WARN=1` in the environment, or add `"CLAUDE_MEMORY_TOMBSTONE_WARN": "1"` to the `env` block of `~/.claude/settings.json`. Do **not** make that change without his say-so.

---

## Rollback

Unsetting `CLAUDE_MEMORY_TOMBSTONE_WARN` disables the check completely, with no code change and no `settings.json` edit. Nothing else in the hook's behaviour is reachable from the tombstone path.
