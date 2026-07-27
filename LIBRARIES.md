# Hermes Agent — Library & Dependency Inventory

**Generated:** 2026-07-27 | **Repository:** [NousResearch/Hermes-Agent](https://github.com/NousResearch/Hermes-Agent) | **Scope:** 174 unique packages (151 NPM + 21 Cargo + 2 PyPI)

---

## Summary

| Registry | Packages | Up-to-Date | Updates Available | Major Gaps |
|----------|---------:|-----------:|-----------------:|-----------:|
| NPM      | 151      | 100        | 51               | 9          |
| Cargo    | 21       | —          | —                | —          |
| PyPI     | 2        | —          | —                | —          |
| **Total**| **174**  |            |                  |            |

> **Major gap** = 2+ major versions behind pinned version.

---

## Notable Upgrade Gaps (Major / Breaking)

| Package | Pinned | Latest | Gap | Risk |
|---------|--------|--------|-----|------|
| cross-env | `^5.1.4` | `10.1.0` | v5 → v10 (5 majors) | ⚠ needs manual bump |
| spectrum-ts | `8.0.0` | `12.4.0` | v8 → v12 (4 majors) | ⚠ pinned exact |
| ps-list | `^6.0.0` | `9.0.0` | v6 → v9 (3 majors) | ⚠ needs manual bump |
| electron | `40.10.2` | `43.2.0` | v40 → v43 (3 majors) | HIGH — API changes across v40→43 |
| typescript | `^6.0.3` | `7.0.2` | v6 → v7 | HIGH — TS v7 breaking syntax |
| express | `^4.21.0` | `5.2.1` | v4 → v5 | HIGH — Express v5 middleware API break |
| pino | `^9.0.0` | `10.3.1` | v9 → v10 | MEDIUM — transport API changes |
| ink | `^6.8.0` | `7.1.1` | v6 → v7 | MEDIUM — component API changes |
| chalk | `^5.4.0` | `6.0.0` | v5 → v6 | MEDIUM |
| dnd-core | `^14.0.1` | `16.0.1` | v14 → v16 (2 majors) | ✅ semver-compatible |
| react-dnd-html5-backend | `^14.0.3` | `16.0.1` | v14 → v16 (2 majors) | ✅ semver-compatible |
| undici | `^6.25.0` | `8.9.0` | v6 → v8 (2 majors) | ⚠ needs manual bump |
| node-addon-api | `^7.1.0` | `8.9.0` | v7 → v8 | MEDIUM — v8 may drop deprecated APIs |
| agent-browser | `^0.26.0` | `0.33.0` | pre-1.0 | MEDIUM — breaking changes expected |

---

## NPM — Production Dependencies

| Package | Pinned | Latest | Status |
|---------|--------|--------|--------|
| @alcalzone/ansi-tokenize | `^0.1.0` | `0.3.0` | 🟡 update |
| @assistant-ui/react | `^0.14.23` | `0.14.28` | ✅ semver |
| @assistant-ui/react-streamdown | `^0.3.4` | `0.3.7` | ✅ semver |
| @vscode/codicons | `^0.0.45` | `0.0.46-24` | ✅ semver |
| @whiskeysockets/baileys | `7.0.0-rc13` | `7.0.0-rc13` | ✅ current |
| @xterm/addon-fit | `^0.11.0` | `0.11.0` | ✅ current |
| @xterm/addon-serialize | `^0.14.0` | `0.14.0` | ✅ current |
| @xterm/addon-unicode11 | `^0.9.0` | `0.9.0` | ✅ current |
| @xterm/addon-web-links | `^0.12.0` | `0.12.0` | ✅ current |
| @xterm/addon-webgl | `^0.19.0` | `0.19.0` | ✅ current |
| @xterm/xterm | `^6.0.0` | `6.0.0` | ✅ current |
| agent-browser | `^0.26.0` | `0.33.0` | 🟡 update |
| auto-bind | `^5.0.0` | `5.0.1` | ✅ semver |
| bidi-js | `^1.0.0` | `1.0.3` | ✅ semver |
| chalk | `^5.4.0` | `6.0.0` | 🟡 major |
| class-variance-authority | `^0.7.1` | `0.7.1` | ✅ current |
| cli-boxes | `^3.0.0` | `4.0.1` | 🟡 major |
| clsx | `^2.0.0` | `2.1.1` | ✅ semver |
| cmdk | `^1.1.1` | `1.1.1` | ✅ current |
| code-excerpt | `^4.0.0` | `4.0.0` | ✅ current |
| d3-force | `^3.0.0` | `3.0.0` | ✅ current |
| dnd-core | `^14.0.1` | `16.0.1` | 🟡 major (semver) |
| dompurify | `^3.4.11` | `3.4.12` | ✅ semver |
| emoji-regex | `^10.4.0` | `10.6.0` | ✅ semver |
| express | `^4.21.0` | `5.2.1` | 🟡 major |
| fflate | `^0.8.3` | `0.8.3` | ✅ current |
| get-east-asian-width | `^1.3.0` | `1.6.0` | ✅ semver |
| gsap | `^3.15.0` | `3.15.0` | ✅ current |
| hast-util-from-html-isomorphic | `^2.0.0` | `2.0.0` | ✅ current |
| hast-util-to-text | `^4.0.2` | `4.0.2` | ✅ current |
| ignore | `^7.0.5` | `7.0.6` | ✅ semver |
| indent-string | `^5.0.0` | `5.0.0` | ✅ current |
| ink | `^6.8.0` | `7.1.1` | 🟡 major |
| ink-text-input | `^6.0.0` | `6.0.0` | ✅ current |
| katex | `^0.16.45` | `0.18.1` | 🟡 update |
| leva | `^0.10.1` | `0.10.1` | ✅ current |
| lodash-es | `^4.17.0` | `4.18.1` | ✅ semver |
| lucide-react | `^0.577.0` | `1.27.0` | 🟡 major |
| mermaid | `^11.15.0` | `11.16.0` | ✅ semver |
| motion | `^12.38.0` | `12.42.2` | ✅ semver |
| nanostores | `^1.2.0` | `1.4.1` | ✅ semver |
| node-addon-api | `^7.1.0` | `8.9.0` | 🟡 major |
| node-pty | `1.1.0` | `1.1.0` | ✅ current |
| pino | `^9.0.0` | `10.3.1` | 🟡 major |
| prism-react-renderer | `^2.3.0` | `2.4.1` | ✅ semver |
| qrcode | `^1.5.4` | `1.5.4` | ✅ current |
| qrcode-terminal | `^0.12.0` | `0.12.0` | ✅ current |
| radix-ui | `^1.4.3` | `1.6.7` | ✅ semver |
| react | `^19.0.0` | `19.2.8` | ✅ semver |
| react-arborist | `^3.5.0` | `3.16.0` | ✅ semver |
| react-dnd-html5-backend | `^14.0.3` | `16.0.1` | 🟡 major (semver) |
| react-dom | `^19.0.0` | `19.2.8` | ✅ semver |
| react-reconciler | `0.33.0` | `0.33.0` | ✅ current |
| react-router-dom | `^7.17.0` | `7.18.1` | ✅ semver |
| react-shiki | `^0.9.3` | `0.11.0` | ✅ semver |
| remark-math | `^6.0.0` | `6.0.0` | ✅ current |
| remend | `^1.3.0` | `1.3.0` | ✅ current |
| semver | `^7.6.0` | `7.8.5` | ✅ semver |
| shiki | `^4.0.2` | `4.3.1` | ✅ semver |
| signal-exit | `^4.1.0` | `4.1.0` | ✅ current |
| simple-git | `^3.36.0` | `3.36.0` | ✅ current |
| spectrum-ts | `8.0.0` | `12.4.0` | 🟡 pinned exact |
| stack-utils | `^2.0.0` | `2.0.6` | ✅ semver |
| streamdown | `^2.5.0` | `2.5.0` | ✅ current |
| strip-ansi | `^7.1.0` | `7.2.0` | ✅ semver |
| supports-hyperlinks | `^3.1.0` | `4.5.0` | 🟡 major |
| tailwind-merge | `^3.5.0` | `3.6.0` | ✅ semver |
| tailwindcss | `^4.2.1` | `4.3.3` | ✅ semver |
| tw-shimmer | `^0.4.11` | `0.4.12` | ✅ semver |
| type-fest | `^4.30.0` | `5.8.0` | 🟡 major |
| undici | `^6.25.0` | `8.9.0` | 🟡 major |
| unicode-animations | `^1.0.3` | `1.0.3` | ✅ current |
| unified | `^11.0.5` | `11.0.5` | ✅ current |
| unist-util-visit-parents | `^6.0.2` | `6.0.2` | ✅ current |
| use-stick-to-bottom | `^1.1.6` | `1.1.6` | ✅ current |
| usehooks-ts | `^3.1.0` | `3.1.1` | ✅ semver |
| vfile | `^6.0.3` | `6.0.3` | ✅ current |
| web-haptics | `^0.0.6` | `0.0.6` | ✅ current |
| wrap-ansi | `^9.0.0` | `10.0.0` | 🟡 major |

### CodeMirror Editor

| Package | Pinned | Latest | Status |
|---------|--------|--------|--------|
| @codemirror/commands | `^6.10.4` | `6.10.4` | ✅ current |
| @codemirror/language | `^6.12.4` | `6.12.4` | ✅ current |
| @codemirror/language-data | `^6.5.2` | `6.5.2` | ✅ current |
| @codemirror/state | `^6.7.0` | `6.7.1` | ✅ semver |
| @codemirror/view | `^6.43.3` | `6.43.6` | ✅ semver |
| @lezer/highlight | `^1.2.3` | `1.2.3` | ✅ current |

### DnD Kit

| Package | Pinned | Latest | Status |
|---------|--------|--------|--------|
| @dnd-kit/core | `^6.3.1` | `6.3.1` | ✅ current |
| @dnd-kit/sortable | `^10.0.0` | `10.0.0` | ✅ current |
| @dnd-kit/utilities | `^3.2.2` | `3.2.2` | ✅ current |

### Docusaurus (Documentation Site)

| Package | Pinned | Latest | Status |
|---------|--------|--------|--------|
| @docusaurus/core | `3.9.2` | `3.10.2` | 🟡 update |
| @docusaurus/plugin-client-redirects | `3.9.2` | `3.10.2` | 🟡 update |
| @docusaurus/preset-classic | `3.9.2` | `3.10.2` | 🟡 update |
| @docusaurus/theme-mermaid | `^3.9.2` | `3.10.2` | 🟡 update |
| @easyops-cn/docusaurus-search-local | `^0.55.1` | `0.55.2` | ✅ semver |
| @mdx-js/react | `^3.0.0` | `3.1.1` | ✅ semver |

### Tauri (Desktop Bridge)

| Package | Pinned | Latest | Status |
|---------|--------|--------|--------|
| @tauri-apps/api | `^2.0.0` | `2.11.1` | ✅ semver |
| @tauri-apps/plugin-dialog | `^2.0.0` | `2.7.2` | ✅ semver |
| @tauri-apps/plugin-opener | `^2.0.0` | `2.5.4` | ✅ semver |
| @tauri-apps/plugin-process | `^2.0.0` | `2.3.1` | ✅ semver |
| @tauri-apps/plugin-shell | `^2.0.0` | `2.3.5` | ✅ semver |

### Three.js (3D Rendering)

| Package | Pinned | Latest | Status |
|---------|--------|--------|--------|
| @react-three/fiber | `^9.6.0` | `9.6.1` | ✅ semver |
| three | `^0.180.0` | `0.185.1` | ✅ semver |

### UI Components & Icons

| Package | Pinned | Latest | Status |
|---------|--------|--------|--------|
| @icons-pack/react-simple-icons | `=13.11.1` | `13.13.0` | ⚠ pinned exact |
| @nous-research/ui | `0.18.2` | `1.5.2` | 🟡 pinned exact |
| @radix-ui/react-slot | `^1.2.4` | `1.3.3` | ✅ semver |
| @tabler/icons-react | `^3.41.1` | `3.45.0` | ✅ semver |
| @tailwindcss/typography | `^0.5.19` | `0.5.20` | ✅ semver |
| @tailwindcss/vite | `^4.2.1` | `4.3.3` | ✅ semver |
| @tanstack/react-query | `^5.100.6` | `5.101.4` | ✅ semver |
| @tanstack/react-virtual | `^3.13.24` | `3.14.8` | ✅ semver |
| @audiowave/react | `^0.6.2` | `0.6.2` | ✅ current |
| @chenglou/pretext | `^0.0.6` | `0.0.8` | ✅ semver |
| @nanostores/react | `^1.1.0` | `1.1.0` | ✅ current |
| @observablehq/plot | `^0.6.17` | `0.6.17` | ✅ current |
| @streamdown/code | `^1.1.1` | `1.1.1` | ✅ current |
| @streamdown/math | `^1.0.2` | `1.0.2` | ✅ current |

---

## NPM — Dev Dependencies

| Package | Pinned | Latest | Status |
|---------|--------|--------|--------|
| @docusaurus/module-type-aliases | `3.9.2` | `3.10.2` | 🟡 update |
| @docusaurus/tsconfig | `3.9.2` | `3.10.2` | 🟡 update |
| @docusaurus/types | `3.9.2` | `3.10.2` | 🟡 update |
| @electron/rebuild | `^4.0.6` | `4.2.0` | ✅ semver |
| @eslint/js | `^9.39.4` | `10.0.1` | 🟡 major |
| @tauri-apps/cli | `^2.0.0` | `2.11.4` | ✅ semver |
| @testing-library/dom | `^10.4.0` | `10.4.1` | ✅ semver |
| @testing-library/react | `^16.3.2` | `16.3.2` | ✅ current |
| @typescript-eslint/eslint-plugin | `^8` | `8.65.0` | ✅ semver |
| @typescript-eslint/parser | `^8` | `8.65.0` | ✅ semver |
| @vitejs/plugin-react | `^6.0.2` | `6.0.4` | ✅ semver |
| concurrently | `^10.0.3` | `10.0.4` | ✅ semver |
| cross-env | `^5.1.4` | `10.1.0` | 🟡 major (5 jumps) |
| electron | `40.10.2` | `43.2.0` | 🟡 major (3 jumps) |
| electron-builder | `^26.8.1` | `26.15.3` | ✅ semver |
| esbuild | `^0.28.1` | `0.28.1` | ✅ current |
| eslint | `^9.39.4` | `10.8.0` | 🟡 major |
| eslint-plugin-perfectionist | `^5` | `5.10.0` | ✅ semver |
| eslint-plugin-react | `^7` | `7.37.5` | ✅ semver |
| eslint-plugin-react-hooks | `^7.0.1` | `7.1.1` | ✅ semver |
| eslint-plugin-react-refresh | `^0.5.2` | `0.5.3` | ✅ semver |
| eslint-plugin-unused-imports | `^4` | `4.4.1` | ✅ semver |
| globals | `^17.4.0` | `17.8.0` | ✅ semver |
| jsdom | `^29.1.1` | `29.1.1` | ✅ current |
| mocha | `10` | `11.7.6` | 🟡 major |
| node-gyp | `^11.4.2` | `13.0.1` | 🟡 major (semver) |
| plist | `^3.1.0` | `5.0.0` | 🟡 major |
| prettier | `^3` | `3.9.6` | ✅ semver |
| ps-list | `^6.0.0` | `9.0.0` | 🟡 major |
| rcedit | `^5.0.2` | `5.0.2` | ✅ current |
| three | `^0.180.0` | `0.185.1` | ✅ semver |
| tsx | `^4.22.4` | `4.23.1` | ✅ semver |
| typescript | `^6.0.3` | `7.0.2` | 🟡 major |
| typescript-eslint | `^8.56.1` | `8.65.0` | ✅ semver |
| vite | `^8.0.16` | `8.1.5` | ✅ semver |
| vitest | `^4.1.5` | `4.1.10` | ✅ semver |
| wait-on | `^9.0.5` | `9.1.0` | ✅ semver |

---

## Cargo (Rust) Dependencies

All 21 Rust crates use feature-based versioning in `Cargo.toml`.

| Crate | Latest | Notes |
|-------|--------|-------|
| tauri-build | `2.6.3` | Tauri v2 build tools |
| tauri | `2.11.5` | Core Tauri framework |
| tauri-plugin-dialog | `2.7.2` | Native file dialogs |
| tauri-plugin-opener | `2.5.4` | Open files/URLs natively |
| tauri-plugin-process | `2.3.1` | Process management |
| tauri-plugin-shell | `2.3.5` | Shell command execution |
| tokio | `1.53.1` | Async runtime |
| futures | `0.3.33` | Futures & streams |
| serde | `1.0.229` | Serialization framework |
| serde_json | `1.0.151` | JSON serialization |
| reqwest | `0.13.4` | HTTP client |
| tracing | `0.1.44` | Instrumentation |
| tracing-subscriber | `0.3.23` | Log subscriber |
| tracing-appender | `0.2.5` | File logging |
| dirs | `6.0.0` | Platform dirs |
| which | `8.0.5` | `which` command wrapper |
| anyhow | `1.0.104` | Error handling |
| thiserror | `2.0.19` | Derived error types |
| once_cell | `1.21.4` | Lazy initialization |
| uuid | `1.24.0` | UUID generation |
| windows-sys | `0.61.2` | Windows FFI |

---

## PyPI (Python) Dependencies

| Package | Constraint | Latest | Notes |
|---------|-----------|--------|-------|
| openpyxl | `>=` | `3.1.5` | Excel file I/O |
| requests | `>=` | `2.34.2` | HTTP client |

---

## Priority Upgrade Recommendations

### Immediate Attention (HIGH risk — breaking changes)

1. **electron** `40.10.2` → `43.2.0` — 3 major versions behind. Desktop app stability risk. Requires testing all native APIs.
2. **typescript** `^6.0.3` → `7.0.2` — v7 may have breaking syntax/type changes. Affects whole codebase.
3. **express** `^4.21.0` → `5.2.1` — v5 drops middleware compatibility. Affects webhook server.
4. **cross-env** `^5.1.4` → `10.1.0` — 5 major versions behind. Dev-only, lower risk.

### Planned Upgrades (MEDIUM risk — semver-incompatible)

5. **ink** `^6.8.0` → `7.1.1` — TUI framework update
6. **pino** `^9.0.0` → `10.3.1` — logging transport API
7. **undici** `^6.25.0` → `8.9.0` — HTTP client
8. **chalk** `^5.4.0` → `6.0.0` — terminal styling
9. **eslint** + **@eslint/js** `^9.x` → `10.x` — linter migration

### Low Effort (semver-safe, just bump range)

10. **agent-browser** `^0.26.0` → `0.33.0` — pre-1.0, test after bump
11. **@nous-research/ui** `0.18.2` → `1.5.2` — internal package
12. **lucide-react** `^0.577.0` → `1.27.0` — icon library major
13. All Docusaurus packages `3.9.2` → `3.10.2`
14. KaTeX `^0.16.45` → `0.18.1`
15. DnD ecosystem `^14.x` → `16.x` (semver-compatible)

---

*Generated from live NPM registry, crates.io, and PyPI API lookups. Report saved to kanban artifacts for reproducibility.*
