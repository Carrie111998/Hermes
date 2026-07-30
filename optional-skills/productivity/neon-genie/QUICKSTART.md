# Neon Genie Quickstart

Governed Hermes skill for opportunity and product intelligence. **Advisory only.**

**Paths:** [How to use (README)](./README.md#how-to-use) · [10-minute demo](./docs/DEMO.md) · [Premiere thesis](./docs/PREMIERE.md) · [Distribution](./docs/HERMES_DISTRIBUTION.md)

---

## Install

**Hermes Skills Hub**

```bash
hermes skills install scrimshawlife-ctrl/Neon-Genie-Hermes/skills/neon-genie
```

**Clone**

```bash
git clone https://github.com/scrimshawlife-ctrl/Neon-Genie-Hermes.git
cd Neon-Genie-Hermes
./install.sh
# → ~/.hermes/skills/neon-genie

python scripts/neon_genie.py do doctor
```

Reload Hermes after install.

---

## Use in Hermes (judgment)

1. Load / trigger **neon-genie**.
2. State the job, constraints, and what “done” looks like.
3. Prefer the smallest profile set; research is proactive unless you turn it off.
4. Expect labeled claims and fail-closed gaps.
5. Open **`run-envelope.json`** first when resuming a packaging workspace.

```text
/neon-genie audit this project using product_architecture, commercial,
and wayfinder_handoff. Research public decision-critical facts. Request
private access instead of inventing. Label OBSERVED / INFERRED /
SPECULATIVE / NOT_COMPUTABLE. Do not modify the repository.
```

```text
/neon-genie zero_option: first cash in 7 days from declared skills only.
No fictional resources.
```

```text
/neon-genie research.enabled=false
```

Request shape: [`templates/request.yaml`](./templates/request.yaml).  
Prose goldens: [`evals/transcripts/`](./evals/transcripts/).  
Behavioral contracts: [`evals/behavioral/`](./evals/behavioral/).

---

## Commands (one pattern)

```text
python scripts/neon_genie.py do <job> [options]
```

| Job | When to use |
|-----|-------------|
| `doctor` | Full smoke (install / CI / agents) |
| `run` | **Start here for packaging** — brief/recipe → workspace + envelope |
| `check` | Skill integrity only |
| `capabilities` | Machine-readable surface (`--json`) for orchestrators |
| `recipe` | Named example end-to-end (`--list` / `--name`) |
| `route` | Profile suggestion from text or brief |
| `validate` | Packet or envelope vs schema |
| `receipt` / `envelope` | Advisory receipt; rebuild `run-envelope.json` |
| `eval` / `transcripts` / `behavioral` | Golden gates, prose, semantic suites |
| `learn` / `reconcile` | PROPOSED ledger + run_id linkage |
| `dist` / `release-check` | Hub mirrors; pre-release version gate |

```bash
python scripts/neon_genie.py help
python scripts/neon_genie.py do run --recipe product-audit --out out/neon-genie/demo
# open out/neon-genie/demo/run-envelope.json first
python scripts/neon_genie.py do capabilities --json
```

Packaging CLI does **not** invent opportunities. Judgment stays in Hermes + `SKILL.md`.

---

## After a real outcome

```bash
python scripts/neon_genie.py do learn --class proof_obtained \
  --summary "…" --envelope out/neon-genie/demo/run-envelope.json
python scripts/neon_genie.py do reconcile \
  --ledger out/neon-genie/learning-ledger.jsonl --runs-root out/neon-genie
```

Entries stay **PROPOSED** — never auto-applied to the skill corpus.

---

## Authority

**May:** research, draft, score, recommend.  
**May not:** spend, publish, contact, mutate repos, promote canon.

Sibling skill: [Kubrick](https://github.com/scrimshawlife-ctrl/Kubrick) (cinematic) — not a dependency.
