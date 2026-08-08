# evaOS Agent 2026.7.20-es.12

- Adds short-lived, profile-authoritative authentication for Pipedream's native MCP without placing developer or provider credentials on customer VMs.
- Runs tools annotated exactly `readOnlyHint: true` directly and routes every write-capable or unannotated MCP call through Hermes' existing approval mode before any connection or RPC.
- Preserves the shared customer gateway, distinct profile homes and LCM databases, per-profile Desktop controls, and the signed Electric Sheep update path.
