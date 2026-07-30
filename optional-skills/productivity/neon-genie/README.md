# Neon Genie

Evidence-bound **product and opportunity intelligence** for Hermes Agent.

| | |
|--|--|
| Authority | `advisory_only` |
| Upstream | https://github.com/scrimshawlife-ctrl/Neon-Genie-Hermes |
| Install (official optional) | `hermes skills install official/productivity/neon-genie` |
| Install (Hub / latest) | `hermes skills install scrimshawlife-ctrl/Neon-Genie-Hermes/skills/neon-genie` |

## What it does

- Product audits and Wayfinder-ready handoffs  
- Opportunity mining, zero-option loops, commercial models  
- Fail-closed evidence: OBSERVED / INFERRED / SPECULATIVE / NOT_COMPUTABLE  
- Public facts → research; private facts → DataRequest  

## Packaging CLI

```bash
python scripts/neon_genie.py do doctor
python scripts/neon_genie.py do run --recipe product-audit --out out/neon-genie/demo
python scripts/neon_genie.py do capabilities --json
```

Downstream consumers open **`run-envelope.json`** first.

Full docs, CI, distribution spine, and releases live **upstream**. This optional tree is a curated install package for the official catalog.
