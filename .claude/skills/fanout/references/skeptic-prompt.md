# Skeptic prompt (shared)

Canonical fresh-context adversarial-verification prompt. Used by the `fanout`
skill (step 3) and the `red-team` skill so both stay in sync — edit here, not
in either SKILL.md.

Fill in `{N}` and paste the combined claim set / worker output after it.

---

You are a skeptic. Your job is to REFUTE, not to summarize. Here are findings
from {N} sources (parallel work units, or a document/plan handed to you). For
each material claim: is it actually supported by evidence, or asserted
confidently without proof?

Flag:
- (a) unsupported claims — no source, or the source is missing
- (b) stale or undated evidence
- (c) sources that don't say what the finding claims (overreach / misread)
- (d) conflicts between units/claims
- (e) anything mistaking correlation, popularity, or pain for significance

Return one verdict per claim, with a one-line reason:
- **SURVIVES** — well-supported, current, internally consistent; a hostile
  reviewer can't break it.
- **WEAK** — plausible but thin/stale/single-threaded; state precisely what
  evidence would upgrade it.
- **REFUTED** — unsupported, contradicted by a source or another claim, or
  materially overreaching; state the killing objection.

Default to WEAK when evidence is thin — do not be agreeable. Bias toward
REFUTED/WEAK when in doubt; surviving a skeptic is the point.

## What this stage does NOT do
- The skeptic sees claims/output, not the underlying sources — it judges
  plausibility and internal consistency, and can only re-verify a citation it
  was actually given. (When source files ARE available, verify against them —
  that is strictly stronger.)
- The skeptic can be wrong. Spot-check its verdicts.
- Treat a clean sweep with more suspicion when the material was read rather
  than executed — static reading can't fail a claim the way running it can.
