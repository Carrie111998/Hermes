# Neon Genie — official optional skill

Evidence-bound product and opportunity intelligence for Hermes.

| | |
|--|--|
| **Upstream** | https://github.com/scrimshawlife-ctrl/Neon-Genie-Hermes |
| **Authority** | `advisory_only` |
| **Folder** | `optional-skills/productivity/neon-genie` |

## Install

```bash
hermes skills install official/productivity/neon-genie
# or community Hub (upstream):
hermes skills install scrimshawlife-ctrl/Neon-Genie-Hermes/skills/neon-genie
```

## Smoke

```bash
python scripts/neon_genie.py do doctor
python scripts/neon_genie.py do run --recipe product-audit --out out/neon-genie/demo
```

Open `run-envelope.json` first when consuming outputs. Maintained upstream.
