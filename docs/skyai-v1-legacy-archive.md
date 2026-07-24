# SkyAI v1 Standalone Repository Archive

Date: 2026-07-24

## Decision

`lomliev/skyvision-hermes-ai-assistant` is the historical standalone
v1/clean-room SkyAI repository. It is archived read-only.

The `skyai_customer` plugin in `lomliev/hermes-agent` is the only canonical
source for:

- SkyAI customer behavior and system-prompt principles;
- public catalog, campaign, support, and voucher facts;
- customer evaluation principles and comparison scenarios;
- chat and voice business-brain semantics;
- future DEV and PROD deployment work.

## Preserved Evidence

Before archival:

- all local and remote Git refs were captured in a verified Git bundle;
- the only dirty worktree was captured as a binary patch;
- those eight uncommitted files were committed on
  `codex/archive-preserve-uncommitted-20260724` at `e6bb160`;
- the preservation tests passed;
- the standalone remote branches and historical voice artifacts remain in the
  archived repository.

The preserved dirty work contained a valid public fact about sequential use of
remaining voucher value. Equivalent residual-voucher facts already exist in
the canonical v2 support evidence. Its keyword expansion, synthetic response
template, and safety-prompt addendum were deliberately not migrated.

## Runtime Provenance At Archive

- Production: SkyAI v2 Hermes, `skyai_customer`, behavior `v2.5`, build
  `df9fbccdc`.
- DEV v2: SkyAI v2 Hermes canary, behavior `v2.4`, build `92fcac46c`.
- Legacy DEV: isolated `skyai_v1` service, no Discord, no customer mutations,
  retained only as frozen comparison evidence.

Archiving the source does not modify or deploy any GCP runtime. Any later
shutdown of the legacy DEV service is a separate infrastructure gate.

## Rules

- Do not unarchive the standalone repository for a normal SkyAI behavior fix.
- Do not deploy from it to DEV or PROD.
- Do not copy v1 classifiers, phrase lists, response templates, or semantic
  safety addenda into v2.
- Revalidate useful business facts and add them to v2 as facts plus general
  Hermes principles.
- Keep SIP/RTP/STT/TTS/PBX transport outside the SkyAI business brain.

## Recovery

If forensic recovery is required, unarchive the GitHub repository or restore
the verified Git bundle into a new local directory. Recovery does not authorize
deployment or production routing.
