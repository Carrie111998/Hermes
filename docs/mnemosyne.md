# Mnemosyne — memory provider evaluation

> **Verdict:** worth shipping as an **opt-in tenth memory provider plugin**, pinned.
> **Not** a default, and **not** a store for CRM/vault content.
> Evaluated 2026-07-25 against `mnemosyne-oss/mnemosyne` @ `main` (v3.15.0,
> `mnemosyne-hermes` 0.5.0). All numbers below were measured locally or read out
> of the project's own README, CI config, changelog, and open issues.

[github.com/mnemosyne-oss/mnemosyne](https://github.com/mnemosyne-oss/mnemosyne) —
MIT, ~1.8k stars, Python ≥3.10, ~42k LOC in the package (~110k with tests and
benchmarks). Billed as "a universal, **Hermes-first** memory layer": one
`pip install`, one SQLite file, no external services.

## Why it's on our radar

It targets Hermes explicitly, and it is architecturally the closest thing to what
our `MemoryProvider` ABC was designed for. It also already exists in this repo's
blast radius: `tests/agent/test_memory_provider.py` carries a regression test for
duplicate `mnemosyne_recall` / `mnemosyne_remember` / `mnemosyne_stats` entries in
the tools array causing 400s from Nous Portal, and `plugins/memory/__init__.py`
uses `load_memory_provider("mnemosyne")` as its docstring example.

## What's genuinely good

- **The test suite is real and it passes.** 2285 passed, 2 skipped, 4 subtests
  passed in 5m23s locally at CI parity. CI runs it on Python 3.10–3.13.
- **Local-first is not just marketing.** I traced the egress paths.
  `core/embeddings.py` defaults `MNEMOSYNE_EMBEDDING_API_URL` to OpenRouter, but
  `_is_api_model()` gates on an explicit opt-in (`openai/` prefix,
  `text-embedding` in the name, a non-OpenRouter base URL, or
  `MNEMOSYNE_EMBEDDINGS_VIA_API=1`). The comment there shows they specifically
  avoided the trap of routing `BAAI/bge-*` to OpenRouter for users who happen to
  have `OPENROUTER_API_KEY` set for chat. Default path is fastembed, on-device.
  Sync is opt-in and offers XChaCha20-Poly1305 client-side encryption.
- **Reasonable security defaults.** GitHub Actions are SHA-pinned. The MCP SSE
  transport refuses to bind a non-loopback host without `MNEMOSYNE_MCP_TOKEN`.
- **Responsive maintainers.** 326 commits in 90 days. Issue #491 (a
  trim-before-embedding foreign-key race) was filed 2026-07-18 and fixed in
  v3.15.0 on 2026-07-20. The CHANGELOG is detailed and honest about regressions.
- **The Hermes integration is thorough, not a stub.** It implements all four
  abstract methods (`name`, `is_available`, `initialize`, `get_tool_schemas`)
  plus most of our optional hooks — `system_prompt_block`, `prefetch`,
  `sync_turn`, `handle_tool_call`, session-switch handling, a write-approval gate
  (`memory.write_approval`), and per-profile bank scoping.

## What stops it being a default

### 1. The headline feature has no CI coverage

`.github/workflows/ci.yml` sets `MNEMOSYNE_NO_EMBEDDINGS: "1"` for the entire
test matrix. The comment is explicit: enabling embeddings "previously caused 48+
failures on the test (3.11) job" via Hugging Face 429s. So the hybrid
vector + FTS5 retrieval that is the entire value proposition is **never exercised
in CI** — only the lexical fallback is verified. All 2285 green tests are green
on the degraded path.

### 2. Degradation is silent, and that is the dangerous kind

Smoke test with five short facts. With the embedding model unreachable (403 in
this sandbox), `remember()` logged `embedding storage failed` per row and stored
anyway; `recall()` then returned lexical results with no signal that hybrid
search was off. On three trivially easy queries it returned **1 of 3**:

| Query | Hits |
|---|---|
| "what env var configures the vault?" (fact says "vault root … `CRM_VAULT_ROOT` environment variable") | **0** |
| "what gates the digest?" | 1 |
| "brewing temperature" (fact says "brewed at 96 degrees celsius") | **0** |

Open issue **#474** is the systemic form of this: `embeddings.available()`
reports `True` while `vec_episodes` is never created, so a silent sqlite-vec
fallback hides a hybrid-retrieval regression. **#518**: an embedding dimension
mismatch is surfaced to the user as "database corrupt". A memory layer that
quietly stops being semantic is worse than one that fails loudly.

### 3. The benchmark claims do not survive reading their own README

- "Top-tier scores" on BEAM end-to-end: **65.2%** — while **Hindsight scores
  73.4%** in their own comparison table. Same table shows a competitor at 90.4%
  on LongMemEval against their 98.9%.
- BEAM retrieval **Recall@10 is 20% at every scale** (100K/500K/1M/10M), framed
  as "recall holds flat across all scales".
- The latency column reads 372ms → 412ms → 493ms → **35ms** going from 100K to
  10M. A 14× speedup at 10× the data is not a result; it's a broken run.
- The repo description claims "sub-millisecond retrieval"; their own table says
  372ms at the smallest scale.
- LongMemEval 98.9% is self-reported on **100 instances** (LongMemEval_S is 500).

Treat the numbers as unaudited marketing. The system may still be good; the
evidence offered is not.

### 4. Half the configuration surface may not be wired

Issue **#482** (maintainer-confirmed: "this is a serious finding"): **50 of 106**
`config.yaml` keys are silently ignored because core modules read env vars into
module-level constants at import time. You can see the pattern directly in
`core/embeddings.py`, where `_OPENAI_API_KEY`, `_OPENAI_BASE_URL` and
`_DEFAULT_MODEL` are bound at import. `config set` writes the key, `config get`
reads it back, and nothing changes.

### 5. Two divergent copies of the Hermes provider ship in one repo

| Path | `__init__.py` | Reached by |
|---|---|---|
| `hermes_memory_provider/` | 3778 lines | `mnemosyne/install.py` (symlinks `~/.hermes/plugins/mnemosyne` → here) |
| `integrations/hermes/src/mnemosyne_hermes/` | 2882 lines | `pip install mnemosyne-hermes` |

`cli.py`, `sync_adapter.py`, `audit.py` and `__init__.py` all differ between the
two. Which code you run depends on which install route you took.

### 6. Tool-schema footprint

`ALL_TOOL_SCHEMAS` exposes **40 tools**, ~**6.5k tokens** of schema on every
request. The README advertises "Hermes Plugin (23 tools)". Our own
`agent/memory_provider.py` docstring names "tool schema bloat" as the reason
`MemoryManager` enforces the one-external-provider limit — this is the largest
provider surface we'd have taken on by a wide margin.

Separately, `mnemosyne_triple_end` is advertised in the MCP `ALL_TOOL_SCHEMAS`
but has no entry in `mcp_tools._TOOL_HANDLERS`; calling it raises
`ValueError: Unknown tool`. Their own 2026-06-09 documentation audit reports
removing this exact ghost tool — from the docs, not from the schema. (The Hermes
path is unaffected: `_handle_triple_end` exists there.)

### 7. Two open issues land directly on us

- **#487** — `mnemosyne-hermes==0.4.0` as the Hermes memory provider causes a
  large gateway RSS increase on the first ordinary turn, in a controlled
  on/off comparison. Still open.
- **#537** — `hermes update`'s SQLite runtime-repair rebuilds the venv and drops
  an externally-installed mnemosyne provider. Filed 2026-07-25.

### 8. Correctness bugs open right now, in the part that has to be trusted

- **#507** — instruction extraction inverts "whenever X" into "**never** X".
- **#506** — sleep-consolidation output outranks its own source memories.
- **#524** — `mnemosyne_invalidate` reports success when no row was invalidated.
- **#525** — `valid_until` mixes local wall-clock timestamps with SQLite UTC.
- **#523** — `QueryCache` persistence bypasses TTL and retains evicted rows.

A memory system that silently inverts a stored instruction is a different class
of problem from one that's merely slow.

### 9. Documentation has a documented history of fabrication

Their own `docs/audit-report-2026-06-09.md` found 71 issues across 75 pages:
"CONFIG_ENTRIES: **7 of 9 env vars were fictional**", a fabricated
`mnemosyne_triple_end` API page, fictional exception classes, fictional `/health`
and `/metrics` endpoints, fictional migration function names. They found and
fixed them — credit for the audit — but the base rate says verify every claim
against source before relying on it.

### 10. Governance and churn

- `CLA.md` grants the project the right to "**re-license Your Contributions under
  any license chosen by the Project, including … any future open-source or
  commercial license**". Today's MIT releases stay MIT; the relicensing option is
  pre-authorized.
- Bus factor ≈ 2: over 90 days, AxDSan (as "Abdias J" + "Abdias Joel") authored
  173 of 326 commits and dplush 50 — ~68% between them.
- `SECURITY.md` points at the retired `AxDSan/mnemosyne` issue tracker and tells
  reporters to "open a GitHub Issue marked as sensitive (GitHub lets you flag an
  issue as confidential when filing it)". **GitHub has no such feature.**
  Following the documented process publishes the vulnerability.
- v3.3.0 → v3.15.0 in roughly four months — a minor release most weeks, on a
  component that owns durable state.
- `core/beam.py` is a single 9,026-line module. 323 `except Exception` handlers
  across the package: consistent with the graceful-degradation design, and also
  the mechanism by which #474-class failures stay quiet.

## Recommendation for hermes-agent

Ship it as a **tenth opt-in provider plugin**, never the default, and only if
someone wants to run it. If we pilot it:

1. **Pin the version.** Weekly minors on a stateful component; take upgrades
   deliberately.
2. **Make the vector path assert, not degrade.** At `initialize()`, verify the
   embedding model actually loaded and that `vec_episodes` exists; log RED and
   fall back explicitly rather than silently (#474, #518).
3. **Trim the tool surface.** 40 tools / 6.5k tokens per request is not
   acceptable as a default. `_configured_tool_schemas()` already supports
   filtering — use it and start with `remember`/`recall`/`stats`.
4. **Watch gateway RSS** across the enable/disable boundary before and after
   (#487).
5. **Expect `hermes update` to drop it** and document the reinstall step until
   #537 is fixed.
6. **Configure by env var, not `config.yaml`,** until #482 lands.

## Recommendation for crm-pipeline

Do **not** put vault content in it. See
[`crm-pipeline/docs/KB_SEMANTIC_INDEX.md`](https://github.com/dizhaky/crm-pipeline/blob/main/docs/KB_SEMANTIC_INDEX.md)
— mnemosyne is architecturally the DAN-2200 "option B" design (local embedding
model + sqlite-vec + FTS5 hybrid fusion, offline), so it is worth reading for
ideas. Adopting it wholesale would import 42k LOC of weekly-churning dependency,
with an uncovered vector path and open data-correctness bugs, into a repo whose
stated posture is zero-runtime-deps, offline, and PII-careful under the D-011
carve-out.

## Reproducing this

```bash
git clone https://github.com/mnemosyne-oss/mnemosyne && cd mnemosyne
python3 -m venv .venv && .venv/bin/pip install -e ".[embeddings,mcp,sync,test]"
.venv/bin/pip install -e ./integrations/hermes

MNEMOSYNE_NO_EMBEDDINGS=1 .venv/bin/python -m pytest tests/ -q   # 2285 passed

# 40 tools, ~6.5k tokens of schema
.venv/bin/python -c "import json,sys; sys.path.insert(0,'integrations/hermes/src'); \
from mnemosyne_hermes.tools import ALL_TOOL_SCHEMAS as T; \
print(len(T), len(json.dumps(T))//4)"

# ghost MCP tool
.venv/bin/python -c "import mnemosyne.mcp_tools as m; m.handle_tool_call('mnemosyne_triple_end', {})"
```
