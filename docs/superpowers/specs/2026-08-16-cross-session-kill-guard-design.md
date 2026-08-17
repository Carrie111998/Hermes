# Cross-session kill guard — design

**Date:** 2026-08-16
**Status:** approved, ready for implementation

## Problem

On 2026-08-16 a detached `python -u -m pytest <repo>/tests/cron -q --timeout=300`
(session `hopeful-proskuriakova-05bba6`) was killed at 20:29:11 EDT, 3m43s into a
1052-test run, with **exit code 15** and no pytest summary — a log that reads
exactly like a hang.

The killer was a **different, concurrent Claude session** (`determined-raman-0c3203`),
which at 20:28:47 ran an ad-hoc memory-relief sweep and reported
`killed: [15676, 31244, 52656]`:

```python
mine = {os.getpid()} | {a.pid for a in psutil.Process(os.getpid()).parents()}
for p in psutil.process_iter(['pid','cmdline']):
    if p.info['pid'] in mine: continue
    cl = p.info['cmdline'] or []
    runner  = any(str(a).endswith('run_tests_parallel.py') for a in cl)
    pytest_w = ('-m' in cl and cl.index('-m')+1 < len(cl)
                and cl[cl.index('-m')+1] == 'pytest')
    if runner or pytest_w: p.kill()      # psutil kill -> exit 15
```

`psutil.Process.kill()` on Windows is `TerminateProcess(handle, SIGTERM)`, hence 15.
Exit 15 is reachable **only** from a Python caller (`psutil.terminate()/kill()` or
`os.kill(pid, SIGTERM)`); taskkill yields 1, `Stop-Process` 4294967295, WMI 0, node 1.
Full measured table: agent memory `exit-code-15-is-a-python-psutil-kill`.

**The guard was the bug.** Excluding `getpid()` + `parents()` protects only the
sweeper's own subtree. On a box that routinely runs 10+ concurrent sessions, this is
a cross-session `pkill -f pytest`. Two earlier variants the same session ran used a
substring test against the *joined* cmdline (`'-m pytest' in cl`), which matched the
sweeper's own `python -c "..."` source text and killed the sweeper itself — those
tool results read literally `Exit code 15`. The rewrite to a structural argv test
fixed self-immolation but not blast radius.

## Non-goal

Hard containment. The offending code was typed into a shell by an agent, so no
repo-side lint can prevent it, and any hook is bypassable by an agent that wants to
bypass it. **The goal is to convert an accident into a deliberate, visible choice.**

## Ownership model (shared by both components)

"Mine" = the process subtree of my own Claude session.

1. Walk `psutil.Process().parents()` upward; the session root is the topmost ancestor
   classified as a Claude session, reusing the live-verified classifier from
   `cull-claude-sessions.py`: `name == claude.exe`, argv contains `--output-format`
   or `claude-code`, argv containing `--type=` excluded (Electron helper).
2. Candidates = that root's recursive descendants.
3. Each parent→child link is validated with `create_time(child) >= create_time(parent)`
   so a dangling or recycled ppid cannot smuggle a foreign process into the subtree.

**Default-deny:** if the session root cannot be resolved, reap nothing and say why.
This mirrors the established convention on this box (`cull-claude-sessions.py`:
unresolvable idle-age → never kill).

Rejected alternative — an `HERMES_TEST_OWNER` env marker stamped at launch. It cannot
work for the actual failure case: both the victim and the sweeper's own strays were
launched ad hoc with no marker, and a guard that only protects cooperating launches
would not have prevented this incident.

## Component A — `scripts/reap_stray_tests.py`

The tool agents reach for instead of hand-rolling psutil.

* Matches test processes **structurally on argv**: `-m` immediately followed by
  `pytest`, or any argv element ending in `run_tests_parallel.py`. Never a substring
  test against the joined cmdline — that is the self-match bug above.
* Never kills: self, own ancestors, anything outside the resolved subtree.
* Always prints a plan (pid, age, RSS, argv head) before executing.
* Kill escalation: `terminate()`, wait, then `kill()`.
* Flags:
  * `--dry-run` — print the plan, kill nothing.
  * `--min-age-minutes N` — only reap processes older than N.
  * `--all-sessions` — box-wide reap for genuine memory emergencies. Prints every
    victim **with its owning session** first, and logs loudly. This keeps the
    emergency path that motivated the original sweep (RAM was at 1.7 GB free) while
    making cross-session killing deliberate rather than accidental.

## Component B — `~/.claude/hooks/block-unscoped-process-kill.py`

`PreToolUse` hook matching `Bash|PowerShell`. First PreToolUse hook on this box;
follows the existing convention (hook scripts in `~/.claude/hooks/*.py`, invoked via
the absolute uv CPython path, colocated pytest file).

**Blocks** (exit 2, reason on stderr naming the reaper) commands pairing process
enumeration with a kill:

| pattern | rationale |
|---|---|
| `process_iter` + `.kill()`/`.terminate()` | the exact 2026-08-16 shape |
| `pkill -f` / `killall` | matches by name across all sessions |
| `Get-Process … \| Stop-Process` | PowerShell equivalent |
| `taskkill /IM` | image name = every instance, cross-session by nature |
| `wmic process where … delete` | WMI equivalent |

**Allows:** targeted kills by explicit PID (`taskkill /PID n`, `Stop-Process -Id n`,
`psutil.Process(<pid>).kill()`), any invocation of `reap_stray_tests.py`, and an
escape marker `cross-session-kill: approved` in the command — following the repo's
existing `# windows-footgun: ok` philosophy.

**Known limits, accepted:** bypassable by marker or obfuscation (by design — see
Non-goal); and it fires on every Bash/PowerShell call box-wide, so a false positive
blocks real work. Mitigated by requiring enumeration *and* kill in the same command,
and by the test table below.

## Testing

TDD: tests written first and proven red against the absent/unfixed implementation.

`tests/scripts/test_reap_stray_tests.py` — `exec()`s the real source with `psutil`
swapped out for a fake process tree (the technique already proven in
`test_cull_claude_sessions.py`, which cannot import its target because it is a flat
top-level script). Cases:

1. a **sibling session's** `-m pytest` survives (the regression this exists for);
2. own-session strays are killed;
3. the substring self-match bug stays fixed — a process whose argv merely *contains*
   the text `-m pytest` inside one element is not matched;
4. unresolvable session root ⇒ nothing killed;
5. `--dry-run` computes a full plan and kills nothing;
6. `--all-sessions` reaches foreign processes and attributes each to its session;
7. `create_time` validation rejects a recycled ppid.

`~/.claude/hooks/test_block_unscoped_process_kill.py` — table-driven block/allow,
including the verbatim 2026-08-16 sweep as a must-block case, each allow-case above,
and a red-proof that neutering the predicate makes the must-block case pass (so the
suite cannot pass vacuously).

## References

* Agent memory `exit-code-15-is-a-python-psutil-kill` — measured exit-code table,
  full incident chain, and everything ruled out with evidence.
* MemPalace `hermes-agent-src/debugging` drawer
  `drawer_hermes-agent-src_debugging_bf501f2f9f9a31634bd20806`.
* `cull-claude-sessions.py` — the session classifier and the default-deny convention.
