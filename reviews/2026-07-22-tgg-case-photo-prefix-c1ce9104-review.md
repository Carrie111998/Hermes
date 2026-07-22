# Cross-provider review: TGG case-photo configured-prefix resolver

- **Verdict: CLEAR** — parent may merge.
- Reviewed ref: `c1ce9104bc7ace6f351b734a1a51ed12d98435d2` (origin/worker/d17d42d8-photo-path, sole commit vs origin/main)
- Reviewer: edna clone, WB `454b929b-1d84-4c2b-9ccf-e82ce5a52b27`, 2026-07-22
- Context: live TGG proof exposed path duplication — configured `media_root` is
  `/home/pclaw/.systems-pcl/data/media/tgg/hermes` while Systems refs are
  `/media/tgg/hermes/<basename>`; the old resolver stripped only `/media` and
  re-joined `tgg/hermes`, producing `<root>/tgg/hermes/<basename>`.

## Scope check

Delta vs origin/main is exactly two files (`tools/pa_business_tools.py` +49/-5,
`tests/test_pa_business_facts.py` +71), all hunks prefix-related: new
`media_ref_prefix` config field + loader + validator, resolver prefix
parameterization, single call-site update, three tests. No runtime, pause,
token, allowlist, or config-file changes.

## Findings

1. **Configured prefix maps directly to root.** With prod-shaped root and
   prefix `/media/tgg/hermes`, ref `/media/tgg/hermes/<basename>` resolves to
   `<root>/<basename>` — no `tgg/hermes` duplication. Verified by executing
   `_resolve_case_photo` directly and via the new handler-level test.
2. **Production config wiring is real.** `deploy/tgg/christopher/config.yaml`
   (the file `TGG_PRODUCTION_CONFIG` reads) already carries
   `media_ref_prefix: /media/tgg/hermes` on origin/main; the new loader test
   asserts the loaded bridge exposes it. All 4 runtime-slot configs carry the
   same value.
3. **Default `/media` behavior preserved.** Dataclass default, resolver param
   default, and `_configured_media_ref_prefix` fallback all yield `/media`;
   pre-existing default-prefix tests still green.
4. **Fail-closed under adversarial input** (probes executed against the live
   functions at the exact commit): wrong prefix, prefix-boundary
   (`/media/tgg/hermesevil/...`), bare prefix, absolute URL, scheme-relative
   URL, query string, percent-encoded traversal, deep `../` traversal to a
   real existing file (caught by `is_relative_to` containment), double-slash
   absolute-relative join, and symlink escape — all REFUSED.
   `_handle_tgg_case_photos` wraps every exception in `tool_error`, so hostile
   refs produce tool errors, not crashes.
5. **Prefix validator fails closed on config.** Refuses `..`, `.`, empty
   segments, non-`/media` namespaces, missing leading slash, bare `/`;
   normalizes trailing slash and whitespace; unset → `/media`;
   `client_bridge.media_ref_prefix` override precedence works.

## Tests

Run in a detached worktree at the exact commit:
- Focused (`-k "media or photo or prefix or production_config"`): 12/12 passed.
- Full `tests/test_pa_business_facts.py`: 70/70 passed.

No maker code was modified by this review.
