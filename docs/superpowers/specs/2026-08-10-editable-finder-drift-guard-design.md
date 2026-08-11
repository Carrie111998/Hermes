# Editable-install drift guard — design

**Date:** 2026-08-10
**Status:** implemented and verified 2026-08-11 (see "Verified" and "Corrections" below)

> Sections below describe the design as approved on 2026-08-10. Six task reviews
> and one end-to-end verification changed several decisions. **Where this
> document and `hermes_cli/install_doctor.py` disagree, the module is
> authoritative.** Known divergences are listed under "Corrections".

## Problem

On 2026-08-10 the installed editable finder held 18 of the 23 top-level packages
`pyproject.toml` declares. Missing: `activity_policy`, `activity_telemetry`,
`devflow_delegation`, `jobflow_dispatch`, `session_bridge`.

`pyproject.toml` declared all 23 correctly. The existing packaging test
(commit b197c5c6d, "catch undeclared top-level packages") validates exactly
that — and passed. The finder is a **separate artifact**, generated once at
install time and never regenerated as packages are added. Declaration
correctness and install correctness are different properties, and only the
first was tested.

The impact was not theoretical. `events/subscribers/jobflow_dispatcher.py`
imports `jobflow_dispatch` at module level, and `events/gateway_integration.py`
imports that subscriber at module level. From any cwd outside `agent-src` the
import chain failed and took down all 13 event-bus subscribers. The running
gateway was healthy only because it happened to have been launched from
`agent-src`.

### The falsifier is a neutral cwd

From inside `agent-src` every package resolves via cwd and the drift is
invisible. Any check must run from somewhere else.

### Current state

The drift has since been repaired by a reinstall. The finder now holds all 37
MAPPING entries (23 packages + 14 `py-modules`) and
`import events.gateway_integration` succeeds from a neutral cwd. **This guard
will therefore pass on this machine today.** It is a regression guard; the
hermetic fixture tests are what prove it detects anything.

## Non-goals

- Repairing drift automatically. The guard reports and names the remedy; it
  does not run `pip install`.
- Guarding non-editable installs against a source tree. A wheel install has no
  source root to diff against; the import-resolvability layer still applies.

## Placement, and why

The check asserts a property of the **developer's environment**, not of the
repo. A plain pytest test would fail for anyone who has not reinstalled, so it
would be written to skip — and then it silently no-ops for every developer and
in CI, checking the property only where it happens to already hold.

So the work splits:

- **The environment assertion lives in a doctor command.** This follows the
  `hermes_cli/events_doctor.py` precedent — a standalone, `python -m`-runnable
  diagnostic, not another section buried in the 2553-line `hermes_cli/doctor.py`.
  `events_doctor` already guards the same bug class (`check_code_drift`: "the
  deployed artifact is not the declared artifact").
- **The checker's logic is covered by a hermetic pytest self-test.** It feeds
  synthetic finder fixtures and injected probe results, so it runs identically
  on every machine and never skips.

## Architecture

New module `hermes_cli/install_doctor.py`, four units with clean seams.

### `declared_names(pyproject_path) -> set[str]`

Pure parse. Returns top-level names from
`[tool.setuptools.packages.find].include` (bare entries and `<name>.*` entries
collapsed to their top-level) **plus** `[tool.setuptools].py-modules`.

Both appear in MAPPING and both fail the same way. `hermes_constants` being
unimportable is as fatal as `jobflow_dispatch`. 23 packages + 14 py-modules =
the 37 MAPPING entries observed.

### `resolve_install_root() -> (Path, str)`

Locates the editable finder for `hermes-agent` in site-packages, parses its
MAPPING, and returns the common parent of its targets plus a provenance string.

**The install root's `pyproject.toml` is the source of truth for "declared"** —
not the cwd repo's. The semantics stay self-consistent: *does the installed
environment expose everything the installed project declares.* Running doctor
from a worktree then reports on the `agent-src` install rather than flagging
phantom drift from worktree-only packages that a reinstall from `agent-src`
would not fix.

Falls back to the repo root of the running `hermes_cli` module when no editable
finder exists (wheel install). Always reports which root it used and why.

**When no root yields a readable `pyproject.toml`** (a sealed wheel install from
PyPI, where no source tree exists), `declared_names` has no input. The breadth
layer is then skipped with an informational note naming the reason — not
reported as a failure, because there is nothing to have drifted from. The depth
layer still runs, since importing `events.gateway_integration` is meaningful on
any install. This is the one case where the guard returns clean without having
checked breadth, and it says so explicitly rather than printing an unqualified
pass.

### `probe(python, names, entrypoints) -> ProbeResult`

Spawns **one** subprocess with `cwd=` a freshly created empty temp dir — not
`%TEMP%` itself, so a stray module cannot perturb the result. In that
subprocess it `find_spec`s every name and imports every entrypoint, emitting
JSON on stdout.

Deliberately **not** `-P`: a real reboot-time launch does prepend cwd to
`sys.path`, and reproducing that faithfully is the entire point of the check.
An empty cwd gives the neutral-cwd falsifier without diverging from how the
gateway actually starts.

The subprocess uses `sys.executable` — the interpreter that invoked doctor,
which is the install being graded. Output names that interpreter so it is never
ambiguous which environment was checked.

`probe` is injected into `run`, so tests never spawn a subprocess.

### `run(...) -> int`

Composes, renders, returns an exit code (0 clean, 1 drift).

## What it asserts, in layers

1. **Breadth.** Every declared name resolves from a neutral cwd. This is the
   property that actually matters, and it is mechanism-independent — editable
   finder, wheel, and `.pth` installs all work. It names exactly which names
   are missing.
2. **Depth.** `import events.gateway_integration` in that same subprocess.
   Catches a broken chain even when every top-level resolves. Kept as a small
   named list, each entry commented with the outage it stands for.
3. **Diagnosis.** Only when an editable finder exists, parse MAPPING to explain
   *why*: "finder holds 18/23; it is generated at install time and is not
   regenerated as packages are added."

Layer 3 **fails soft**. The MAPPING regex is coupled to setuptools' generated
file format; a regex miss must degrade the explanation, never the guard. Layers
1 and 2 carry the verdict.

The regex tolerates the annotation being present or absent:

```
MAPPING\s*(?::\s*dict\[[^\]]*\])?\s*=\s*(\{.*?\})\n
```

Matching a bare `MAPPING = ` against the current generated file returns `None` —
the annotation is required for today's setuptools and optional for tomorrow's.
Parsed with `ast.literal_eval`, not `eval`.

## Failure output

The message names the remedy, the interpreter, and the trap:

```
pip install -e . --no-deps
```

run from `C:\Users\diego\.hermes\agent-src` under the Python 3.11 WindowsApps
interpreter that owns the install.

Plus the console-script lock: a gateway launched via `hermes.exe` stays
resident as the **parent** of its python process and blocks `pip install -e .`
with `WinError 32`. Launching the gateway as `python -m hermes_cli.main` avoids
that entirely.

## Testing

`tests/hermes_cli/test_install_doctor.py`, fully hermetic — synthetic finder
files and injected probe results, no subprocess, no dependency on the
developer's install:

- A finder fixture holding 18 of 23 reports **exactly** the five known names.
- A finder written **without** the `: dict[str, str]` annotation still parses.
  (The regex gotcha becomes a test case rather than a comment.)
- A garbage/unparseable finder degrades the diagnosis but still reports drift
  from the probe layers.
- `declared_names` picks up `py-modules` as well as packages.
- The rendered failure text contains the remedy command.

Before this is called done, the guard is additionally demonstrated against a
deliberately drifted **copy** of the real finder, to show it fires on the real
artifact shape and not just on fixtures.

## Wiring

`hermes doctor` gains an "Install Integrity" section calling the same core,
wrapped in `try/except` like the existing security-advisory section, so a bug
here cannot block the rest of doctor. It costs roughly one second for the
subprocess — acceptable, because that subprocess *is* the check.

Two surfaces, one core, different failure conventions:

- **Standalone** (`python -m hermes_cli.install_doctor`) returns exit code 0
  when clean and 1 on drift, matching `events_doctor`. This is the form a
  future laptop-monitor probe or CI step would call.
- **`hermes doctor`** does not exit on drift. It renders the section and appends
  a remediation line to doctor's existing `issues` list, so drift surfaces in
  the standard summary block alongside every other finding.

## References

- MemPalace: wing `hermes-agent-src`, room `jobflow-event-dispatch`, drawer
  `23c5ef47f46a11562d207913`
- Agent memory: `reference-editable-finder-drift-and-console-script-lock`
- Existing packaging test: `tests/test_packaging_metadata.py` (commit b197c5c6d)
- Precedent: `hermes_cli/events_doctor.py` `check_code_drift`

---

## Verified 2026-08-11

### The MAPPING parse reads the real generated artifact

The live finder holds **37** MAPPING entries (23 packages + 14 `py-modules`, as
predicted). A copy with the five 2026-08-10 names deleted holds **32**, and
`resolve_install_root` reads that copy correctly — resolving the root to
`C:\Users\diego\.hermes\agent-src` from the MAPPING targets even though the
file itself sat in a temp directory, and reporting exactly
`activity_policy`, `activity_telemetry`, `devflow_delegation`,
`jobflow_dispatch`, `session_bridge` as absent.

One correction to the plan's script: `re.sub` with a plain replacement string
mangles the Windows backslashes in `repr(mapping)` as escape sequences. The
replacement must be a lambda.

### The breadth and depth layers fire against the real interpreter

Running the probe with `PYTHONNOUSERSITE=1` (suppressing the user-site finder,
so the interpreter is genuinely blind) reported **37 of 37 declared names
missing** and `events.gateway_integration` failing with
`ModuleNotFoundError: No module named 'events'` — the real chain from the
outage, through real import machinery rather than a fixture.

### The verification found a defect the fixtures could not

The first run of the above printed:

    Why: The editable finder holds 37 of the 37 declared names.
    Remedy — regenerate the finder by reinstalling the editable package:
        pip install -e . --no-deps

That diagnosis contradicts the FAIL above it, and the remedy is wrong:
reinstalling regenerates a finder that is already complete. A finder that
**lists** every name that failed to resolve is not stale — it is not being
**loaded**. Real causes: a different interpreter than the one owning the
install, user site-packages disabled (`PYTHONNOUSERSITE=1` / `python -s`), or a
deleted `.pth`. A Windows service or Scheduled Task with a scrubbed
environment reaches this state without anyone intending it.

Fixed in `50edfc58e`. `Findings` gained `finder_is_stale: bool | None` —
`True` when the finder omits a missing name, `False` when it lists them all,
`None` when undetermined. `analyze` selects the matching diagnosis, and
`remedy_lines` returns not-loaded guidance instead of the reinstall block when
it is `False`. `render` and `doctor_section_lines` both pass the flag through,
so the two surfaces cannot give contradictory advice. Undetermined still
defaults to the reinstall remedy.

This is the whole argument for keeping the end-to-end step: every fixture in
the suite passed while the guard was giving misleading remediation on the one
scenario the fixtures did not model.

### Regression suite

`tests/hermes_cli/test_install_doctor.py` + `tests/test_packaging_metadata.py`
→ **53 passed**. The existing packaging tests are untouched; this guard is
additive and covers a different property.

Against the unmodified environment the guard exits **0** and reports
`[OK] every declared package resolves from a neutral cwd`, which is correct —
the drift was repaired before this work began.

---

## Corrections (2026-08-11)

Recorded rather than silently edited, so the reasoning that changed is visible.
The module is authoritative wherever this document still says otherwise.

### Signatures that changed

- `resolve_install_root()` returns an **`InstallRoot` dataclass**
  (`path`, `provenance`, `mapping`), not the `(Path, str)` tuple described
  above. The provenance string has to travel with the path to every consumer,
  and a tuple made that easy to drop.
- `probe(names, entrypoints, python=None, env=None)` — not
  `probe(python, names, entrypoints)`. The `env` seam was added so the guard
  could be proven end-to-end against the real interpreter under
  `PYTHONNOUSERSITE=1`.
- `remedy_lines(install_root, finder_is_stale=True)` gained its second
  parameter with the not-loaded fix.
- `Findings` has **six** fields: the five described plus `finder_is_stale`.

### "Tests never spawn a subprocess" is wrong

The design said `probe` is injected everywhere so no test spawns a subprocess.
Three tests deliberately DO spawn one, against stdlib names and a
guaranteed-missing name. That is the better decision and it stays: the
neutral-cwd guarantee is the foundation the whole guard rests on, and mocking
it would only test the mock. `test_probe_runs_from_a_directory_that_is_not_the_repo`
plants a decoy module in the caller's cwd and proves the child cannot see it.
Those tests remain hermetic — they need no editable install. Slowest individual
test call is ~2.3 s against a per-test 30 s cap.

### The doctor remediation goes to `manual_issues`, not `issues`

`issues` is doctor's auto-fixable list. Editable-install drift cannot be
auto-fixed, so it belongs in `manual_issues`.

### The cost estimate was wrong by ~8x

"Roughly one second for the subprocess" is not true. Measured:
`doctor_section_lines()` takes **~8.5 s**, because the depth layer imports
`events.gateway_integration`, which pulls in 13 subscribers, 5 producers, and
the bus. That is the honest price of the layer that actually reproduces the
outage, and it is paid on every `hermes doctor` run. Recorded here so nobody
later "optimizes" the depth layer away believing it was free. If that cost ever
needs bounding, add a breadth-only quick path rather than deleting depth —
breadth is the mechanism-independent layer, but depth is the one that caught
the real chain.

### A Critical defect the final review found

`resolve_install_root` used the MAPPING parse for **two** purposes: computing
the install root (a verdict input, since `_collect` reads
`root.path / "pyproject.toml"` for `declared`) and producing the diagnosis. So
an unparseable MAPPING returned `path=None`, which left `declared=None`, which
SKIPPED breadth, which made `ok` True — and `run()` exited **0**.

That violated this document's own layering rule, and the consequence was
severe: the first time setuptools changed its generated-file format, the guard
would have become a permanent silent no-op reporting success. The exit code is
the machine-readable channel a monitor or CI step consumes; prose notes are not.
The guard's own failure mode was the bug class it exists to catch.

Fixed in `1ad9fec41`: the unparseable branch now falls back to the running
module's repo root for declarations while keeping `mapping=None`, so breadth
still runs and only the diagnosis degrades. Verified: an unparseable finder now
yields `checked_breadth=True`, 37 missing, `ok=False`, `finder_is_stale=None`.

**The layering rule needs restating, because the original wording was too
narrow.** "The MAPPING parse must never affect the verdict" was read as "the
`diagnosis` field is the only thing gated on it" — but the parse also fed the
root, and therefore the verdict, through a second path. The rule is: *no value
derived from the MAPPING parse may reach the breadth or depth layers.*

### Also fixed in the same pass

- `doctor_section_lines` prefixed every remediation with "Editable install has
  drifted", contradicting the not-loaded remedy that followed it — the same
  contradiction `50edfc58e` fixed, surviving at the other surface.
- The doctor summary item was a 1140-character single line (the whole remedy
  block space-joined); it is now a short line pointing at
  `python -m hermes_cli.install_doctor` for the full text.
- Two tests would have passed against the Critical defect above; both now pin
  that breadth was actually checked.
- `find_editable_finder` selected the lexicographically-last finder, so
  `0_9_0` outranked `0_10_0`. Now ordered by parsed numeric version — 0.20.0 is
  the next minor, so this was close.
