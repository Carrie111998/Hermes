# server/webui — design context

## Running the impeccable skill

**Always `cd server/webui` first.** The skill's loader reads
`process.cwd()` with no override (`loadContext(process.cwd())` in
`scripts/load-context.mjs`, no argument, no env var).

```bash
cd server/webui && node "<skill>/scripts/load-context.mjs"
```

Run from the repo root and it picks up the root `PRODUCT.md`, which is a
35KB **engineering** spec (API routes, data contracts, sprint order) with no
register, users, brand or tone. The skill will silently proceed with useless
context.

The design-context files live here:

- `server/webui/PRODUCT.md` — register (`product`), users, purpose, brand
  personality, anti-references, design principles, accessibility.
- `server/webui/DESIGN.md` — the visual system. Source of truth is
  `css/tokens.css`; DESIGN.md explains the reasoning. If they disagree, the
  CSS wins.

Do not move or merge these into the repo root. The root `PRODUCT.md` is a
different document that must stay intact.

## Non-obvious constraints in this UI

- **Mono-dominant type.** `body` inherits JetBrains Mono. Sans is opt-in:
  Satoshi for display, Inter for prose via the `.ifz-prose` allowlist in
  `app.css`. New paragraph surfaces must join that list or they render
  monospace.
- **CSP is `font-src 'self'`.** Fonts must be self-hosted in `fonts/`. CDN
  links fail silently to Arial.
- **woff2 needs an explicit MIME registration** (`mimetypes.add_type` in
  `server/app.py`); stdlib has no woff2 entry on Windows and browsers drop the
  preload on type mismatch.
- **The nav rail and the login screen are always ink**, in both themes.
  Anything inside them must use `--sidebar-*` tokens; page-surface tokens leak
  through as light chips on a dark rail.
- **No shadows, `border-radius: 0`, Signal Blue under ~10% of a screen.**
  Inherited house rules, not per-feature preferences. See DESIGN.md.


<claude-mem-context>

</claude-mem-context>