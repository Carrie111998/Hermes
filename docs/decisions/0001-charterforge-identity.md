# Decision 0001: Charterforge Identity

- Status: accepted
- Date: 2026-07-27
- Scope: independent autonomous-company runtime

## Decision

The project is named **Charterforge**.

Canonical naming is:

| Surface | Name |
|---|---|
| Product and application | Charterforge |
| Repository | `charterforge` |
| Python distribution | `charterforge` |
| CLI | `charterforge` |
| Python namespace | `charterforge` |
| Environment prefix | `CHARTERFORGE_` |
| POSIX state root | `~/.charterforge` |
| Windows state root | `%LOCALAPPDATA%\charterforge` |
| Service/container prefix | `charterforge-` |
| Metrics prefix | `charterforge_` |
| Default log identity | `charterforge` |

Hermes names may remain temporarily as documented migration aliases when
removing them would strand existing installations. They are legacy
compatibility surfaces, not the project identity.

## Rationale

“Charter” represents the standing mission, authority, constraints, and success
criteria that govern autonomous action. “Forge” represents building and
operating the company. The name works as a product name and as a lowercase
identifier without implying official affiliation with Hermes Agent.

No authoritative replacement name was found in committed files, decision
records, or project history before this decision.

## Collision check

On 2026-07-27, exact-name checks returned no project at:

- `https://pypi.org/pypi/charterforge/json`
- `https://registry.npmjs.org/charterforge`
- `https://api.github.com/repos/charterforge/charterforge`
- `https://hub.docker.com/v2/repositories/library/charterforge/`

General web searches for `"Charterforge"` also did not identify a conflicting
software or autonomous-company runtime. This is a reasonable engineering
check, not trademark clearance or a guarantee that no unindexed use exists.

## Independence and attribution

Charterforge is an independent derivative of Hermes Agent. The root
[attribution document](../../ATTRIBUTION.md) and original license are
load-bearing project records and must remain present.

