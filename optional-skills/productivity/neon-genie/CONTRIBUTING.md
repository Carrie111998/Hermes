# Contributing to Neon Genie

Thank you for helping keep Neon Genie rigorous and advisory-only.

## Principles (do not “simplify” away)

1. **Advisory only** — never grant spend/publish/mutate ([ADR 0001](docs/adr/0001-advisory-only-authority.md)).
2. **Skill contract is the product brain** — Python is packaging only ([ADR 0002](docs/adr/0002-skill-contract-is-product-brain.md)).
3. **Hub mirrors are generated** — edit root sources; run `distribution_spine.py write` ([ADR 0003](docs/adr/0003-hub-mirror-strategy.md)).
4. **Wayfinder boundary** — Neon Genie owns intent; Wayfinder owns execution planning ([ADR 0004](docs/adr/0004-neon-genie-wayfinder-boundary.md)).
5. **Claim labels** — never invent OBSERVED ([ADR 0005](docs/adr/0005-claim-label-ontology.md)).

## Development

```bash
python scripts/neon_genie.py do doctor
python scripts/distribution_spine.py write   # after schemas/profiles/scripts/examples change
python scripts/release_check.py              # before tagging
```

### PR checklist

- [ ] `do doctor` green
- [ ] `distribution_spine.py verify` green after any packaging change
- [ ] No new domain profiles unless justified (prefer stabilize)
- [ ] CHANGELOG entry for user-visible changes
- [ ] VERSION / SKILL / manifest stay aligned when releasing

### Sensitive paths (extra care)

- `SKILL.md`
- `schemas/` · `references/schemas/`
- `references/gates.yaml`
- `distribution.yaml`
- `.github/workflows/`
- `docs/adr/`

## Release process

1. Bump `VERSION`, `manifest.json`, `SKILL.md` frontmatter, `CHANGELOG.md`, `README` badge.
2. `python scripts/distribution_spine.py write`
3. `python scripts/release_check.py`
4. Merge to `main` (CI: Hermes Skill Evals).
5. Tag and push:
   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```
6. The **Release** workflow builds the hub tarball, checksum, and GitHub release notes from CHANGELOG.

Manual dry-run: Actions → Release → Run workflow (`dry_run: true`).

## Feedback

Use GitHub issue templates:

- Operator outcome
- Behavior regression
- Hub install failure
- Schema / recipe proposal

Learning ledger entries remain **PROPOSED** and never auto-apply to the corpus.
