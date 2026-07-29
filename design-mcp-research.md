# Design & Creative Tools MCP Research Report
**Date:** June 3, 2026
**Context:** macOS-based AI agent producing Instagram carousels, social media graphics, and web designs. Currently hand-coded SVGs — laborious and quality-limited.

---

## 1. FIGMA MCP SERVERS (The #1 priority)

### 1A. Official Figma MCP Server (by Figma Inc.)
- **Package:** `figma-developer-mcp` / `https://mcp.figma.com/mcp` (Streamable HTTP)
- **Stars:** 1,530 (guide repo), official Figma product
- **Capabilities:**
  - **Read:** `get_design_context` (AI-optimized node payload), `get_metadata`, `get_variable_defs` (design tokens), `get_screenshot`, `search_design_system`
  - **Write (beta):** `use_figma` — runs arbitrary Figma Plugin API JavaScript to create/modify frames, components, variables, auto layout, text
  - **Design system:** Code Connect for component mapping, variable definitions
- **Maturity:** **Stable** — official product, but "write to canvas" is in beta (currently free, will become paid)
- **Setup:** Extremely simple — `npx figma-developer-mcp --figma-api-key=KEY --stdio` or add URL `https://mcp.figma.com/mcp` to any MCP client. OAuth for web clients (Claude.ai, v0).
- **Cost:** Free tier (6 calls/month for Starter/View seats). Dev/Full seats on Pro/Org/Enterprise get tiered rate limits. Write-to-canvas currently free during beta.
- **Quality improvement:** HUGE — native Figma objects with real fonts, auto-layout, proper typography, design system components. Far beyond hand-coded SVGs.

### 1B. Figma Console MCP (southleft) — Best for Design Systems
- **Package:** `figma-console-mcp`
- **Stars:** N/A (newer project), **106 tools** (largest Figma MCP tool surface)
- **Capabilities:**
  - Bidirectional token sync (export Figma variables → DTCG / CSS / Tailwind / SCSS / TS / JSON; push code-side edits back)
  - Component analysis, variant state machines, WCAG 2.2 linting, accessibility audits
  - Version history diffing, changelog generation, node blame
  - FigJam board creation (stickies, flowcharts, tables)
  - Figma Slides authoring (create/reorder slides, transitions)
  - Design system inventory (unified extraction of tokens + components + styles)
  - **Write:** 95 tools via NPX/Cloud Mode, creates frames, components, variables
  - **22 standalone skills** (markdown playbooks) available separately
- **Maturity:** **Stable** (v1.29.2, actively maintained, MIT license)
- **Setup:** `npx figma-console-mcp` with `FIGMA_ACCESS_TOKEN`. Desktop Bridge plugin for writes. Cloud Mode for web AI clients (no Node.js).
- **Cost:** Free. Requires Figma Desktop for write operations.
- **Quality improvement:** HUGE — the most comprehensive design-systems tooling. Perfect for maintaining brand consistency across carousels. Export tokens → use for consistent styling.

### 1C. Framelink MCP (GLips/Figma-Context-MCP) — Most Popular
- **Package:** `figma-developer-mcp` (same npm name as official, but this is the community original)
- **Stars:** 14,967 ⭐ (most starred Figma MCP)
- **Capabilities:** Read Figma layout data, simplify/reduce context for AI coding agents. Primarily **read-only** — fetches optimized design specs for code generation.
  - Tools: `get_design_context`, `get_metadata`, `download_figma_images`
- **Maturity:** **Stable** — very mature, widely used
- **Setup:** `npx figma-developer-mcp --figma-api-key=KEY --stdio`
- **Cost:** Free (REST rate limits apply based on Figma plan)
- **Quality improvement:** Good for design-to-code, but lacking write capability for creating carousels directly.

### 1D. Figma UI MCP (TranHoaiHung) — Bidirectional, No API Key
- **Package:** `figma-ui-mcp`
- **Stars:** N/A (newer, npm package)
- **Capabilities:** **Full bidirectional bridge.** AI draws UI on Figma canvas AND reads designs back. No Figma API key needed — works over localhost plugin bridge.
  - **Write:** `figma_write` — frames, shapes, text, prototypes, auto-layout, gradients, SVG paths, components, instances, variables, prototyping (setReactions), component properties, icon libraries
  - **Read:** `figma_read` — node trees, colors, typography, screenshots, CSS, resolved variables, instance overrides
  - **Info:** `figma_status`, `figma_docs`, `figma_rules` (design system rule sheets)
  - **Docs tool:** Full Figma Plugin API reference accessible to AI
- **Maturity:** **Active development** (v2.5.26, many releases per week)
- **Setup:** Simplest of all — `npx figma-ui-mcp`. No API key, no env vars.
- **Cost:** Free
- **Quality improvement:** **EXCELLENT** — best option for creating professional carousels with proper typography, layout, and visual hierarchy in native Figma. No API limits.

### 1E. Plumb MCP (tathagat22) — With Verification Loop
- **Package:** `plumb-mcp`
- **Stars:** 58
- **Capabilities:** 14 tools including READ + VERIFY. Unique feature: `plumb_verify` tool that diffs rendered code against Figma design (color deltas, no pixel diff). Can work headless from .fig files.
  - Plugin-based (no rate limits), works on Free plan
  - Compact design spec output (not verbose JSON)
  - SVG/PNG asset export
- **Maturity:** **Stable** — well-documented, MIT license
- **Setup:** `npm i -g plumb-mcp` → `plumb-mcp init`. Figma plugin needed.
- **Cost:** Free
- **Quality improvement:** Good for design-to-code verification. Not ideal as primary carousel creation tool.

### 1F. Figsor — Chat-Driven Design Creation
- **Package:** `figsor` (npm) / GitHub: AsifKabirAntu/figsor
- **Stars:** 72
- **Capabilities:** Bridges Cursor to Figma for chat-driven design creation and editing directly on your Figma canvas. Two-way communication.
- **Maturity:** **Experimental**
- **Cost:** Free
- **Quality improvement:** Good — direct Figma creation via natural language.

### 1G. Write-Only Figma MCP Server (oO)
- **Package:** Local git install `figma-mcp-write-server`
- **Stars:** 23
- **Capabilities:** 24 tools focused on **write operations** through Figma Plugin API (bypasses REST API limits). Core Design, Layout, Design System, Boolean Ops, Vectors, Developer Tools.
- **Maturity:** **Pre-release** (< v1.0.0)
- **Setup:** Moderate — requires cloning, building, installing Figma plugin manually
- **Cost:** Free
- **Quality improvement:** Good for programmatic carousel creation, but not as polished as figma-ui-mcp.

### 1H. Free/Unlimited Figma MCPs (No API Key)
- **figma-mcp-but-free:** Read/write via plugin bridge, no API key, no rate limits
- **@vkhanhqui/figma-mcp-go:** 73 tools, Go-based, no API key, no rate limits
- **@impeterwayne/figma-mcp-android:** Read-only via plugin, 21 tools, no rate limits
- **Maturity:** **Experimental** — community projects

---

## 2. CANVA MCP & API

### Canva MCP Server (@mcp_factory/canva-mcp-server)
- **Package:** `@mcp_factory/canva-mcp-server`
- **Latest:** v1.0.0
- **Capabilities:**
  - `canva_list_users_me`, `canva_list_me_profile` — user info
  - `canva_list_designs` — list/search designs
  - `canva_create_designs` — create new designs (doc, presentation, whiteboard, custom size)
  - `canva_get_designs` — get design metadata
- **Maturity:** **Early / Experimental** — only 5 tools, very basic
- **Setup:** OAuth authentication (Canva Client ID + Secret)
- **Cost:** Free (uses Canva Connect API, which is free but has rate limits)
- **Quality improvement:** Minimal currently. Can create designs but lacks rich manipulation (layers, text, elements). Not useful for carousel creation yet.

### Canva Apps SDK (not MCP, but relevant)
- Canva has a rich Apps SDK (`@canva/app-ui-kit`, `@canva/design`, `@canva/platform`) for building apps *inside* Canva
- No official MCP integration yet
- Canva Connect API (REST) can automate design creation but has limited manipulation capabilities
- **Verdict:** Don't prioritize — MCP support is too immature

---

## 3. PENPOT MCP (Open Source Figma Alternative)

### 3A. Official Penpot MCP Server (@penpot/mcp)
- **Package:** `@penpot/mcp` (npm) / `penpot/penpot-mcp` (GitHub)
- **Stars:** 308 ⭐
- **Capabilities:**
  - Read/write/create Penpot design elements via Plugin API
  - Architecture: MCP Server ↔ WebSocket ↔ Penpot Plugin ↔ Penpot Document
  - LLM writes/executes arbitrary JavaScript via Penpot Plugin API
  - Streamable HTTP + SSE endpoints
  - ~20 tools (design-to-design, code-to-design, design-code workflows)
- **Maturity:** **Stable** — official Penpot product
- **Setup:** Moderate — requires running npm install + npm run bootstrap, loading plugin in Penpot in browser, connecting MCP client via proxy
- **Cost:** **Free** (Penpot is open source, self-hostable)
- **Quality improvement:** Excellent for an open-source workflow, but adds self-hosting complexity. If the team already uses Penpot, this is a strong option.

### 3B. Montevive Penpot MCP (Python-based)
- **Package:** `penpot-mcp` (PyPI) / `montevive/penpot-mcp`
- **Stars:** 228 ⭐
- **Capabilities:** Alternative Python implementation, connects directly to Penpot REST API
- **Setup:** `pip install penpot-mcp` or `uvx penpot-mcp`
- **Cost:** Free
- **Quality improvement:** Good Python-based alternative. More suited for developers than designers.

### 3C. Self-Hosted Penpot MCP (ancrz) — 68 Tools!
- **Package:** `ancrz/penpot-mcp-server` (GitHub)
- **Stars:** 8
- **Capabilities:** **68 tools** — the largest Penpot tool surface. Tri-layer access: direct PostgreSQL reads, RPC API writes, browser plugin bridge for live canvas operations.
  - Full project management, shape/text manipulation, component creation, export (SVG/PNG), CSS generation, design token management
- **Maturity:** **Experimental** (new, but ambitious)
- **Setup:** Complex — requires self-hosted Penpot + Docker + PostgreSQL access
- **Cost:** Free
- **Quality improvement:** Very comprehensive but high setup barrier.

---

## 4. IMAGE GENERATION MCPs

### 4A. OpenAI Image Generation (@mindstone/mcp-server-openai-image)
- Generate images via DALL-E 3 / OpenAI image API
- **Setup:** Simple — API key needed
- **Cost:** Pay-per-use (OpenAI API pricing)
- **Use case:** Generate carousel background images, illustrations

### 4B. Together AI Image Generation (together-mcp)
- Multiple open models via Together AI
- **Setup:** Simple — API key needed

### 4C. Google Imagen (maagpi-images-mcp)
- Generate/edit/describe images via Imagen and Gemini

### 4D. Gency AI (@gency-ai/gency-mcp)
- Product image generation, commercial use
- **Cost:** Paid

### 4E. mcp-pix-tools
- Programmatic image generation without AI costs — barcodes, word clouds, palettes, charts
- **Cost:** Free (no AI API needed)
- **Useful for:** Quick programmatic assets, no AI generation cost

---

## 5. EXCALIDRAW & DIAGRAMMING MCPs

### 5A. Excalidraw MCP (excalidraw-mcp)
- **Package:** `excalidraw-mcp`
- **Capabilities:** Create/update/delete Excalidraw elements (rectangles, ellipses, diamonds, text, arrows, lines, images), query/filter elements, group/align/distribute/lock elements, scene management, theme control
- **Maturity:** **Stable**
- **Setup:** `npx excalidraw-mcp` — dead simple
- **Cost:** Free
- **Use for:** Wireframes, rough diagrams, flowcharts, not production carousels. Great for brainstorming layout ideas.

### 5B. Excalidraw MCP Server (multiple variants)
- `excalidraw-mcp-server` — security-hardened with API key auth, rate limiting
- `excalidraw-mcp-sentinel` — SQLite persistence, multi-tenancy
- `@cmd8/excalidraw-mcp` — simpler variant
- `mcp-excalidraw-server` — real-time canvas, WebSocket sync
- **All free, open source**

### 5C. Embedded Editor for Claude Code
- **Package:** `embedded-editor-for-claude-code`
- Combines Excalidraw, tldraw, Markdown, DuckDB in one MCP workspace
- **Use for:** Planning/ideation workspace alongside design work

---

## 6. DESIGN SYSTEM / BRAND MANAGEMENT MCPs

### 6A. Design Extract / designlang (Manavarya09)
- **Package:** `designlang` (npm)
- **Stars:** 3,021 ⭐
- **What it does:** Points headless browser at any URL, extracts complete design system: DTCG tokens, Tailwind config, shadcn theme, Figma variables, motion tokens, component anatomy, brand voice, WCAG contrast scoring
- **Outputs:** 17+ files per run
- **Maturity:** **Stable** (v12.8)
- **Setup:** `npx designlang https://example.com` — trivial
- **Cost:** Free (open source)
- **Quality improvement:** **MASSIVE** for brand consistency — extract any website's design system in one command. Can also `designlang mcp` to expose as MCP server. Clone sites as Next.js starters.
- **Perfect for:** Capturing brand palettes from competitor sites, generating consistent design tokens for carousel creation

### 6B. Storybook MCP (@storybook/mcp & @storybook/addon-mcp)
- **Package:** `@storybook/mcp`, `@storybook/addon-mcp`
- **What it does:** Serves component knowledge from Storybook stories/documentation to AI agents. Helps write and test stories automatically.
- **Use for:** Web design workflows where component libraries are documented in Storybook
- **Setup:** Moderate — requires Storybook 8+ with manifests
- **Cost:** Free

---

## 7. ADOBE CREATIVE CLOUD MCP

**No Adobe MCP servers exist** in npm, GitHub, or the MCP registry as of June 2026. Adobe has no official MCP support. The Adobe APIs (Photoshop, Illustrator, Premiere Pro) are available via REST/SDK but no one has wrapped them as MCP tools yet.

---

## 8. OTHER NOTABLE CREATIVE TOOLS

### 8A. Canva MCP
- Very early stage (5 basic tools, v1.0.0)
- Can create designs but not manipulate elements
- **Not ready for production carousel creation**

### 8B. Chrome DevTools MCP & Playwright MCP
- **Packages:** `chrome-devtools-mcp`, `@playwright/mcp`
- Can control browsers for web design capture, screenshot generation
- Useful for capturing web designs for inspiration/reference

### 8C. @siemens/element-mcp
- Design system component MCP
- Niche use case

---

## RECOMMENDED STACK FOR YOUR USE CASE

### Primary Recommendation: Figma + figma-ui-mcp + designlang

For creating professional Instagram carousels with proper typography, layout, and visual hierarchy:

1. **figma-ui-mcp** (`npx figma-ui-mcp`) — **START HERE**
   - **Pros:** No API key, no rate limits, full read/write, bidirectional, 4 tools covering creation + reading, actively developed (v2.5.x)
   - **Setup:** `npx figma-ui-mcp`, install Figma plugin, done
   - **Why:** AI agent can draw frames, text, shapes directly on Figma canvas with real fonts, auto-layout, proper spacing. This is the closest to "magic" — tell your agent "create a 5-slide Instagram carousel about crypto market analysis" and it builds it in Figma with proper design.
   - **Carousel workflow:** Create 1080×1920 frames → add text with typography styles → fill with gradient backgrounds → export as PNGs → arrange as carousel

2. **designlang** (`npx designlang`) — for brand consistency
   - Extract any brand's design system into DTCG tokens, Tailwind config, CSS variables
   - Feed extracted tokens into your carousel design workflow
   - Also works as MCP server (`designlang mcp`)

3. **Official Figma MCP** (`https://mcp.figma.com/mcp`) — complementary
   - For `get_design_context`, `get_screenshot`, `search_design_system` when you need to read existing Figma designs
   - Best for design-to-code workflows

### Secondary Recommendations:

4. **Figma Console MCP** — if you need advanced design system management (token sync, version history, WCAG audits, FigJam boards)
5. **Penpot MCP** — if you want open-source, self-hosted alternative (still early)
6. **Excalidraw MCP** — for rapid wireframing and layout ideation before final polish in Figma
7. **OpenAI Image MCP** — for generating carousel background images and illustrations
8. **mcp-pix-tools** — for programmatic charts, word clouds, and infographic elements

### What NOT to use:
- **Canva MCP** — too early, limited tools
- **Adobe MCP** — doesn't exist
- **Hand-coded SVGs** — obsolete once you have any Figma MCP working

### Quickstart for your agent:

```json
// Add to your MCP client config:
{
  "mcpServers": {
    "figma-ui": {
      "command": "npx",
      "args": ["-y", "figma-ui-mcp"]
    },
    "design-extract": {
      "command": "npx",
      "args": ["-y", "designlang", "mcp"]
    }
  }
}
```

Then your agent can:
1. Extract brand design systems with `designlang`
2. Create professional carousels in Figma with `figma-ui-mcp`
3. Export slides as PNGs for Instagram upload

**Total cost: $0** (all recommended tools are free/open source)
