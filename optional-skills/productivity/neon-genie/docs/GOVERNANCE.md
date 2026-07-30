# Repository governance

## Branch protection (recommended)

| Rule | Setting |
|------|---------|
| Default branch | `main` |
| Require status checks | `eval` (workflow: Hermes Skill Evals) |
| Require branch up to date | yes |
| Force push | denied |
| Branch deletion | denied |
| PR required | yes (solo maintainers may use admin bypass carefully) |
| Delete head branch on merge | yes |

Sensitive paths (review extra carefully): `SKILL.md`, schemas, `distribution.yaml`, gates, CI, ADRs.

## Release automation

- Tag `vX.Y.Z` → `.github/workflows/release.yml`
- Pre-flight: `scripts/release_check.py`
- Artifact: `skills/neon-genie` tarball + sha256
- Notes: extracted from `CHANGELOG.md`

## Canon and learning

- Learning ledger: PROPOSED only, `auto_apply_forbidden: true`
- Promote patterns only via reviewed PRs + releases
- No autonomous corpus mutation
