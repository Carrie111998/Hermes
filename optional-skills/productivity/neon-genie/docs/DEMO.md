# Neon Genie — 10-minute demo

From zero to a governed packet path. **Advisory only.**

See also: [README — How to use](../README.md#how-to-use) · [PREMIERE.md](./PREMIERE.md)

## Prerequisites

- Git, Python 3  
- Hermes (optional for chat; packaging demo is CLI-only)  

## Minute 0–2: Install

```bash
git clone https://github.com/scrimshawlife-ctrl/Neon-Genie-Hermes.git
cd Neon-Genie-Hermes
./install.sh
# → ~/.hermes/skills/neon-genie

python scripts/neon_genie.py do doctor
```

Expected: full smoke `PASS` at current VERSION.

## Minute 2–4: Operator run (preferred path)

```bash
python scripts/neon_genie.py do run --recipe product-audit --out out/neon-genie/demo-audit
ls out/neon-genie/demo-audit
```

You should see:

| File | Role |
|------|------|
| **`run-envelope.json`** | **Open this first** — canonical entry point |
| `profile-route.json` | Smallest profile set |
| `product-packet.stub.json` | Boundary + `completion_proof` / `proof_path` |
| `data-requests.json` | Private access request |
| `wayfinder-handoff.stub.json` | Intent freeze flag |
| `run-receipt.json` | Advisory receipt |
| `HERMES_NEXT.md` | Judgment stays in Hermes + SKILL.md |

```bash
python scripts/neon_genie.py do validate \
  --packet out/neon-genie/demo-audit/run-envelope.json \
  --type envelope --strict-authority
```

## Minute 4–6: Zero-option honesty

```bash
python scripts/neon_genie.py do run --recipe zero-option --out out/neon-genie/demo-zero
```

Empty skills/access → honest **`NOT_COMPUTABLE`**, not invented capabilities.

Optional:

```bash
python scripts/neon_genie.py do run --recipe commercial --out out/neon-genie/demo-commercial
python scripts/neon_genie.py do capabilities --json | head -40
```

## Minute 6–8: Gates & behavioral suite

```bash
python scripts/neon_genie.py do eval
python scripts/neon_genie.py do behavioral --suite
```

## Minute 8–10: Outcome ledger (linked to run_id)

```bash
python scripts/neon_genie.py do learn \
  --class proof_failed \
  --summary "demo: no real cash yet; packaging path verified" \
  --envelope out/neon-genie/demo-audit/run-envelope.json \
  --ledger out/neon-genie/learning-ledger.jsonl

python scripts/neon_genie.py do reconcile \
  --ledger out/neon-genie/learning-ledger.jsonl \
  --runs-root out/neon-genie
```

Ledger entries are **PROPOSED** only — never auto-canon.

## What you just proved

1. Install + doctor green  
2. Packaging run emits a **consumer-facing envelope**  
3. Fail-closed zero-option path  
4. Semantic behavioral gates  
5. Learning linked to `run_id` without corpus mutation  

Next: real Hermes chat with the [worked example](../README.md#worked-example-before--after), or [CONTRIBUTING.md](../CONTRIBUTING.md) for releases.
