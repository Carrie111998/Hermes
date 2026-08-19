# Characterization gate: version tolerance and provider scoping

**Date:** 2026-08-19
**Status:** patch tolerance REJECTED (unchanged); provider scoping BUILT 2026-08-19,
together with the `bridge_revision` field its §6 recommendation made a precondition -- see §7 onward
**Code touched by this document:** none of the gate's comparison logic

## 1. What prompted this

`resolve_characterization_gate()` (`session_bridge/characterize.py`) compares the newest
characterization report's `versions` map against `_current_cli_versions()` with a plain `!=`
over the whole dict. Any change in either CLI raises
`CharacterizationGateError("version_drift", "characterization_version_mismatch")`, which makes
`scripts/install_session_bridge.ps1` refuse to install.

On 2026-08-19 the only delta was `codex-cli 0.146.0 -> 0.147.0`; Claude was unchanged at
`2.1.216 (Claude Code)`. Recovery required `characterize --provider all`, which creates and
disposes a real Claude session *and* a real Codex session, because
`SessionBridgeBackend.characterize()` rejects any `--provider` other than `all`.

Two questions were asked: can the gate tolerate patch-level drift, and can a refresh be scoped
to the provider that actually drifted?

## 2. What the gate actually buys

Worth establishing before arguing about loosening it. The gate has exactly two effects:

1. **Boolean admission.** It blocks install, and blocks `serve` when
   `config.mirrors.automatic_creation` is set (`cli.py`). Uninstall is *not* gated —
   `Assert-AgentAndCharacterization` is called only inside `if (-not $Uninstall)`.
2. **`gate.codex_registration_turn_required`**, echoed in `characterize`'s own JSON output and
   read nowhere else. It is informational. The value the coordinator actually acts on is
   observed live per placeholder creation (`coordinator.py`, `used_registration_turn`).

So the gate is a freshness assertion — "the installed CLI versions have been proven to drive
this bridge" — not a source of runtime configuration. Do not defend or weaken it on the
registration-turn flag.

## 3. Patch-level tolerance: REJECTED

The test was: *has any characterized behavior ever changed within a patch release?* It has,
twice, in this repository's own history.

| Commit | Delta | Consequence |
| --- | --- | --- |
| `73bf34bbb7` "support Claude Code 2.1.216" (2026-07-21) | `2.1.110 -> 2.1.216`, patch | 421 insertions over 7 files, including `tests/session_bridge/test_claude_visibility_characterization.py` |
| `b8e901c960` "recognize Claude 2.1.219 main prompt" (2026-07-27) | `2.1.216 -> 2.1.219`, patch | Claude changed its REPL footer; `claude_registrar.py` regex loosened from `"⏵⏵\s*don't\s*ask\s*on"` to `"⏵⏵\s*don't\s*ask(?:\s+on)?\b"` |
| `abef384bed` "anchor Claude readiness to mode indicator" (2026-07-27) | same line again | loosened to bare `"⏵⏵"` five days later |

`claude_registrar` is on the characterization path (`characterize.py` imports it), and a
sibling constant is named `_CLAUDE_2110_RESUME_SCAFFOLD`. The characterized surface — native
session file layout, discovery, `--resume` semantics, REPL readiness text, Codex rollout
format — is **undocumented vendor internals**. Semver covers none of it.

Three further reasons, each independently sufficient:

1. **It would not have fixed its own motivating incident.** `0.146.0 -> 0.147.0` is a *minor*
   bump under semver (`0.MINOR.PATCH`). A patch-tolerant gate blocks that drift identically.
2. **Wrong provider.** Across all 17 reports on this machine, Claude drifts by patch
   (`2.1.110 -> 2.1.216`, then static for a month) and Codex drifts by minor
   (`0.120.0 -> 0.144.4 -> 0.146.0 -> 0.147.0`). One of four drift events in the gate's first
   five weeks was patch-level, on the provider that is currently frozen.
3. **The observable is not version-deterministic.** Codex `0.144.4` produced one pass and two
   failures; `0.146.0` produced one pass and three failures. Same version, different outcome —
   a flake cannot be told from a regression in this data, so the hypothesis is untestable here
   regardless of the two proofs above.

**Do not re-propose patch tolerance without new evidence.**

## 4. Provider-scoped refresh: the design

Unlike version tolerance, this does not weaken the version comparison at all. It changes the
*granularity* of the claim from "this pair of versions was proven together" to "this version of
this provider was proven".

### 4.1 Why it is semantically honest

`run_live_characterization()` characterizes the two providers in **independent** `try` blocks:
separate adapters, separate failure capture, no shared mutable state beyond the report dict,
the shared title/marker/cwd, and a Codex-only origin guard. No cross-provider interaction is
measured today. A report that records both providers is therefore two independent proofs that
happen to share a file.

### 4.2 Required changes

1. **Report schema v1 -> v2.** `_validate_gate_report` currently requires
   `providers == {"claude", "codex"}` exactly, so a single-provider report is *malformed*. v2
   must permit a non-empty subset while keeping every existing per-provider field validation.
2. **Per-provider gate resolution.** Replace "newest report overall, must match both versions"
   with, for each provider P: the newest valid report that records P, must record P at the
   installed version of P, and must pass for P. Both providers must resolve for the gate to
   pass.
3. **Relax `characterization_requires_all_providers`** in `SessionBridgeBackend.characterize()`
   to accept `claude` / `codex`, and thread the selection into `run_live_characterization`.
4. **Origin-guard interaction.** `load_codex_characterization_origins` must keep counting every
   valid report, including Claude-only ones that carry no Codex block. Its "every valid report
   counts, including failed characterizations" invariant must survive v2 unchanged — a native
   ID recorded by any report must never be treated as native user work.
5. **Installer messaging.** The gate diagnostic already names the drifted provider (see §5);
   with scoping it should also name the scoped refresh command.

### 4.3 Invariants that must be preserved

- A newer *failing* report for provider P at version V must still block P at V. Per-provider
  resolution keeps this only if "newest recording P" is evaluated before the pass check —
  taking the newest *passing* report would resurrect a proof a later run disproved.
- Malformed or redirected report files must still fail the whole gate closed, not just the
  provider they mention.
- `created_at` (not mtime) remains the ordering key.

### 4.4 The cost, which the §3 finding makes worse

A pair report guarantees both proofs came from **one bridge revision**. Provider scoping gives
that up: after a Codex-only refresh, the standing Claude proof may be weeks old. §3 shows
Claude-handling code churns hard — `2.1.110`, `2.1.216`, `2.1.219` each forced bridge changes —
so the stale half is exactly the half most likely to have gone stale in code rather than in
version.

Neither the current gate nor this design records a bridge revision, so *no* report is
protected against bridge-code drift. Scoping does not introduce that hole; it widens it. A
`bridge_revision` field in the report, gated against the installed checkout, would close it and
would make scoping strictly safer than the status quo. That is a larger change and is not
proposed here.

## 5. What was implemented instead

Two changes that reduce the cost of drift without touching the comparison:

1. **`-WhatIf` no longer runs the version gate** (`install_session_bridge.ps1`). A preview
   applies nothing, so gating it protects nothing; blocking it only removed the ability to
   inspect a pending install while the drift stood. Every real install still runs the gate.
   The structural checks above it (agent root, report root, `uv`, bridge executable) still run
   under `-WhatIf`, and `$script:UvPath` / `$script:BridgeExecutable` are still assigned, so
   the launcher preview stays truthful.
2. **`describe_characterization_gate()`** (`session_bridge/characterize.py`) resolves the gate
   and returns `(exit_code, message)`. On drift the message names *which* provider moved and
   both sides of the comparison, and marks the unchanged provider as unchanged. The installer
   calls it and includes the diagnostic in its throw instead of an opaque one-liner.

Both are covered by tests written first: six pytest cases in
`tests/session_bridge/test_live_characterization.py`, and a rejected-gate scenario in
`scripts/test_install_session_bridge.ps1` asserting that `-WhatIf` survives, changes no bytes,
and that a real install is still blocked with the diagnostic attached.

## 6. Recommendation

Leave the exact-equality comparison alone. Build provider scoping only alongside a
`bridge_revision` field; scoping on its own trades a two-session refresh for a proof that can
silently age past the code it was proving.

---

# Implementation, 2026-08-19

**Status of this document:** §3 (patch tolerance REJECTED) stands unchanged. §6's
condition — build scoping *only* alongside `bridge_revision` — was met: both landed
together, `bridge_revision` first.

## 7. What `bridge_revision` records

`current_bridge_revisions()` (`session_bridge/characterize.py`) returns, per provider,
a `sha256:` digest of the bridge modules that implement how the bridge drives and reads
that provider. Content-addressed, not git-derived: an editable install runs the working
tree, so a commit id would call an uncommitted change unchanged. Line endings are
normalised, so a checkout with different EOL settings is not read as a behaviour change.

The manifest (`BRIDGE_REVISION_MODULES`) is the transitive top-level intra-package import
closure of each provider's adapter modules (`BRIDGE_REVISION_ROOT_MODULES`), stopping at
`BRIDGE_REVISION_EXCLUDED_MODULES`, plus `characterize` itself as the shared harness:

| provider | modules |
| --- | --- |
| claude | `characterize`, `claude_adapter`, `claude_registrar`, `claude_visibility`, `claude_visibility_codes`, `models` |
| codex | `characterize`, `claude_adapter`, `codex_adapter`, `models` |

Excluded: `store`, `context_pack`, `sidebar`, `sidebar_placement`,
`sidebar_reconciliation` — the catalog/mirroring surface. Live characterization never
opens a store, builds a context pack or places a sidebar; it creates a placeholder,
discovers the native session file, reads it, resumes it and disposes of it. They enter
the closure only as type and helper imports, and `store.py` alone took 109 commits in
the seven weeks from 2026-07-01, so tracking it would invalidate every proof for changes
it never measured.
`test_bridge_revision_manifest_covers_the_characterization_import_closure` re-derives the
closure from the installed source, so a new import into an adapter fails the suite until
it is either tracked or deliberately excluded.

## 8. The mismatch policy, and why it is two-tier

A digest mismatch is fatal **only for a proof that is not the newest report on disk** —
which, before scoping, could never happen. The newest report is exempt.

The evidence that forced this shape, measured over `git log --since=2026-07-01`:

| module | commits in 7 weeks |
| --- | --- |
| `claude_registrar` | 47 |
| `codex_adapter` | 49 |
| `characterize` | 25 |
| `claude_adapter` | 17 |
| `claude_visibility` | 16 |
| whole `session_bridge/` package | 389 |

A `bridge_revision` checked absolutely — report digest must equal installed digest, for
every proof — would reject the gate roughly twice a day on the Claude manifest alone.
Each rejection demands a live run that creates and disposes real provider sessions. That
is not a gate anyone pays; it is a gate everyone bypasses. It would also newly block
what the pair-report gate has always allowed: reusing yesterday's proof against today's
bridge.

So the check is applied exactly where scoping creates new exposure:

- **Tier 1 — the newest report.** Not revision-checked. This is the pre-existing,
  accepted risk: a proof taken at one revision and reused as the bridge moves forward.
  Unchanged from the status quo, by design.
- **Tier 2 — any older report a scoped resolution falls back to.** Revision-checked, and
  fatal on mismatch (`CharacterizationGateError("revision_drift", …)`). This is the hole
  §4.4 identified: a Claude proof left standing by a Codex-only refresh, asserted over
  Claude-handling code that has since changed.

The result is the bar §6 set — scoping is **strictly safer** than the status quo, never
laxer: every configuration the old gate accepted is still accepted, and the one new
configuration scoping introduces carries a check the old gate had no way to make.

A v1 report carries no `bridge_revision`, so it can satisfy Tier 1 but never Tier 2:
scoped reuse of a pre-schema-v2 proof fails closed.

**Practical consequence, stated plainly:** given the churn above, a Claude proof is often
already revision-stale by the time Codex drifts, so a scoped refresh will frequently be
refused with `revision_drift` and a full refresh demanded anyway. That is the honest
answer, not a defect — the alternative is asserting Claude compatibility over bridge code
that changed underneath it. The saving is real when the drifting week's commits landed
outside the other provider's manifest.

## 9. Schema v2

```json
{
  "schema_version": 2,
  "characterization_id": "…",
  "created_at": "…+00:00",
  "automatic_mirroring_enabled": false,
  "versions":        {"codex": "codex-cli 0.147.0"},
  "bridge_revision": {"codex": "sha256:…"},
  "providers":       {"codex": {…}}
}
```

`versions`, `bridge_revision` and `providers` must carry **identical, non-empty** key
sets, a subset of `{claude, codex}`. v1 reports stay valid and keep their old rules
(both providers, no `bridge_revision`) — one malformed file fails the whole gate closed,
so re-versioning the existing report root would have bricked it.

## 10. Gate resolution

For each provider P: the newest valid report **recording** P must pass for P, at P's
installed CLI version, and — if it is not the newest report on disk — at the installed
bridge revision for P. Both providers must resolve. "Newest recording P" is evaluated
before the pass check, so a later failing run still buries an earlier passing one
(§4.3). `created_at` remains the ordering key; malformed or redirected files still fail
the whole gate, not just the provider they mention.

`CharacterizationGate` gained `provider_characterization_ids`;
`codex_registration_turn_required` now comes from the report that actually proved Codex.

## 11. Surface changes

- `characterize --provider {all,claude,codex}` (`all` remains the default), threaded to
  `run_live_characterization(providers=…)`, which preflights, characterizes, and records
  only the selected providers. A Claude-only run prepares no Codex origin guard.
- `load_codex_characterization_origins` tolerates reports with no Codex block and still
  counts every valid report's native ID. A Codex origin guard naming a report that
  records no Codex block is unexplainable provenance and fails closed.
- `describe_characterization_gate()` accepts `current_bridge_revision`, describes each
  provider from the report that would satisfy it, names the providers whose standing
  proof predates the installed bridge, and offers the **scoped** refresh command when
  exactly one CLI drifted *and that scoped refresh would actually clear the gate*.
  The second condition matters: a scoped refresh demotes the current report, so the
  other provider's half of it then faces the Tier-2 revision check. If that half could
  not survive it -- a stale digest, or a v1 report with no digest at all, which is every
  report on this machine today -- the diagnostic keeps recommending `--provider all`
  rather than spending a real session to arrive at the same place.
  `scripts/install_session_bridge.ps1` (in `~/.hermes`) needed no change: it prints
  whatever the diagnostic returns.
- New stable error code `revision_drift`, surfaced by `characterization_status()` and as
  `RolloutGateBlocked("characterization_revision_drift")`.

## 12. Verification

Written test-first; 70 pass in `tests/session_bridge/test_live_characterization.py`,
including the two skipped live-only cases. **No live characterization was run** —
the scoped-run tests replace every seam that reaches a provider CLI
(`resolve_cli_executable`, `_cli_version`, `_characterize_claude`, `_characterize_codex`)
and assert which provider paths were entered.
