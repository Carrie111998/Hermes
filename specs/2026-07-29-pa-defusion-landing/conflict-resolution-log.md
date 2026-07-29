# PA de-fusion Phase 1 landing-reconcile conflict log

**WB:** `1a61fede-2fea-406c-8c72-b1d4ed1bb05e`  
**Reconcile base:** Hermes `origin/main` at
`08efbfb67b3c69400dd2845a8e805296e3de6287`  
**Held branch:** `origin/worker/41cb9170`

## Branch reconciliation

- Branch A (`origin/worker/ef7660ab` at `bcff03f4ba`) required no replay:
  every Branch-A commit was already an ancestor of the reconcile base.
- Branch B (`origin/worker/77854585` at `0f2eb822b8`) was replayed onto the
  reconcile base as `2d7287b160`. The cherry-pick was textually clean.
- Both original branch refs were checked on the remote after the replay and
  remained at their original SHAs.
- Systems `c85cd9a7` and marshal `531fd40df9` are already ancestors of their
  current `origin/main` refs. Neither repository was changed.

## 2026-07-28 increment intersections

| Increment | Intersection | Resolution |
|---|---|---|
| `c30b9cb686` / `2b413df2a7` spreadsheet and document retention | `gateway/durable_jsonl_consumer.py` had acquired a shared-plane dependency on `validate_tgg_retainable_document`; `tools/pa_business_tools.py` carried a TGG-named gate in a shared module. | `28f2048bb1` added the tenant-neutral `validate_retainable_document`, switched the consumer and spreadsheet path to it, and retained the TGG-named function only as a compatibility facade. Neutral-path regression coverage was added. |
| `2606b28422` sandbox-path annotation | Overlaps the Phase-1 shared-tool plane, but its behavior is parameterized by supplied tenant/agent metadata rather than a TGG literal. | Preserved unchanged; relevant sandbox tests passed on the final stack. |
| `d3fc0bd8c4` manifest refresh | Manifest content intersects the final assembled runtime. | Ran the canonical generator. It reported 578 files and produced no diff. The bundle dry-run found the same 578-file set. |
| `daa65fa9b5` `client_url` producer | Shared business-fact output. | Preserved unchanged; the producer remains config-driven. |
| `1f7437c250` `media_ref` promotion and document delivery | Shared document-delivery path. | Preserved unchanged after neutralizing the validator call described above; focused document and consumer tests passed. |
| `8bc917de55` SQLite dataset materialization | Shared materialization path. | Preserved unchanged; selection remains configuration-driven via dataset `type: sqlite`. |
| Branch B replay envelope | Branch A had temporarily dual-written neutral and `_tgg_*` metadata. | `2d7287b160` makes neutral keys canonical, reads neutral keys first with legacy fallback, and keeps explicit legacy fixture coverage rather than retaining a live TGG write path. |

## Explicitly not treated as 2026-07-28 intersections

`TGG_*` environment names, the `/var/lib/tgg-capture` path check, raw
TGG/Christopher SQL, and TGG allowlists in shared business tools predate the
seven increments audited above. They remain Phase 3/5 de-fusion surfaces under
the ratified plan. This reconcile did not silently normalize them, and did not
expand Phase 1 to remove them.

## Scope checks

- No runtime module moved or was renamed.
- The only new path in the final stack is a replay test fixture.
- The preserved MTU commit `b9c6ab5431` was not included.
- No main branch, deployed runtime, client host, or MTU process was changed.
