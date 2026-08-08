---
title: "feat: Turn the Artifacts tab into a Hermes-themed .md file editor with a save hotkey"
status: draft
date: 2026-08-07
type: feature
target_repo: hermes-agent
origin: ZM32 (Clairvoyance ops) — wants to open on-disk .md files from the Artifacts tab in a Hermes-themed editor and save with a hotkey, instead of the tab launching the OS default handler.
---

# feat: Artifacts tab → Hermes-themed .md file editor with save hotkey

## Summary

Make a `.md` file artifact opened from the **Artifacts tab** land inside a
Hermes-themed editor (rendered preview + editable source + save), rather than
being handed to the OS default app. Add a `mod+s` save hotkey while the editor
is focused.

The structural finding that de-risks this: **the editor already exists.**
`apps/desktop/src/app/chat/right-rail/preview-file.tsx` is a full on-disk
file preview/editor — it reads files from disk, renders markdown (Streamdown +
Shiki, mermaid/svg through the shared `RichCodeBlock`), switches
rendered/source/diff, enters edit mode, writes back via `writeDesktopFileText`,
and tracks dirty state. It is already themed with Hermes `--ui-*` design tokens.
This plan **routes the Artifacts tab into that existing editor** and adds the
missing save hotkey — it does not build a new editor.

---

## Problem Frame

The **Artifacts tab** (`src/app/artifacts/index.tsx`) is a session-artifact
gallery. It collects `{image,file,link}` records from the last 30 sessions and,
for a file record, `openArtifact()` calls `openExternal(href)` (Artifacts
`index.tsx:246-267`). On-disk `.md` files therefore open in the OS default
markdown handler — never in Hermes. The result is exactly what the user calls
"a kind of useless artifacts tab" for their use case (curating `.md` pipeline
docs, contracts, and specs on the Clairvoyance box).

Meanwhile the right-rail file **preview pane** already implements everything the
user is asking for. The disconnect is purely that the Artifacts tab and the
preview/editor are not connected, and the editor has no save *hotkey* (save is
button-only, `EditControls` → `saveEdit()`).

Theming is a non-issue: both surfaces already share the Hermes token system, so
"editor themed to Hermes" is satisfied by reusing `preview-file.tsx` components.

---

## Requirements

- R1. Opening a `.md` (and any plain-text) **file** artifact from the Artifacts
  tab opens the Hermes file editor, not the OS default handler.
- R2. The editor supports **rendered preview**, **source**, and **edit** modes
  for the opened file (reuse `preview-file.tsx`).
- R3. Editing is writable back to disk via the existing
  `writeDesktopFileText` path; a dirty file is not silently discarded.
- R4. A **`mod+s` save hotkey** saves the edited file while the editor has
  focus (and does not steal `mod+s` from other surfaces when the editor is not
  active).
- R5. File artifacts without a resolvable on-disk path (e.g. remote-gateway
  artifacts, or artifacts whose `value` is not a local file path) still fall
  back to the current download / `openExternal` behavior.
- R6. Focused tests cover the artifact→editor routing and the save hotkey.

---

## Key Technical Decisions

- **Reuse, don't rebuild.** The `.md` renderer (`MarkdownPreview`,
  `MARKDOWN_COMPONENTS`), mode switcher (`PreviewModeSwitcher`), source view
  (`SourceView`), edit controls, and write path all already exist in
  `preview-file.tsx`. The implementation routes the Artifacts-tab open action
  into the preview pane's existing file editor instead of introducing a second
  editor.
- **Artifacts tab opens the preview/editor pane, targeted at the file.** Reuse
  the same preview `PreviewTarget` store (`@/store/preview`) that the right
  rail uses, so opening from Artifacts and opening from a session produce the
  same editor with zero duplication. Where the existing artifact open path
  already builds a `PreviewTarget`, route through it; otherwise construct the
  same `PreviewTarget` from the artifact record's `value`/`path`.
- **Save hotkey already exists — no new keybind.** The `CodeEditor` used by `LocalFilePreview` binds `Mod-s` → `save` → the editor's `onSave` prop, and `preview-file.tsx` wires that to `saveEdit()` (`preview-file.tsx:965`). So saving by `mod+s` while the editor is focused already works in both local and remote. The scope therefore shrank: the implementation is **one routing change** (U1) plus its tests; U2 became a verification-only note.
- **Fallback preserved.** If the artifact has no local resolvable path, keep the
  existing `downloadGatewayMediaFile` / `openExternal` behavior (R5).

---

## High-Level Technical Design

```mermaid
flowchart LR
    A[Artifacts tab<br/>file row click] --> B{value = resolvable<br/>local file path?}
    B -- yes --> C[Open file editor in preview pane<br/>PreviewTarget{type:file, path}]
    B -- no --> D[existing download / openExternal fallback]
    C --> E[MarkdownPreview / SourceView / edit mode]
    E --> F[mod+s keybind active on focus]
    F --> G[writeDesktopFileText → save]
```

- Artifacts `openArtifact()` for a file record with a local path calls the
  preview-pane open path (new thin helper, e.g. `openFileInPreview(path)`) that
  opens the right-rail preview targeted at that file and focuses the editor.
- The file editor already handles disk read, markdown render, edit, dirty state,
  and save. Only the `mod+s` handler is net-new.

---

## Implementation Units

### U1. Route file artifacts from the Artifacts tab into the file editor

**Goal:** Clicking a `.md`/text file in the Artifacts tab opens the Hermes file
editor instead of the OS default handler.

**Requirements:** R1, R2, R5

**Dependencies:** None

**Files:**
- `apps/desktop/src/app/artifacts/index.tsx`
- `apps/desktop/src/app/artifacts/artifact-utils.ts`
- `apps/desktop/src/store/preview.ts` (only if a small `openFileInPreview`
  helper is the cleanest home; otherwise colocate in artifacts)

**Approach:**
- In `openArtifact()` (Artifacts `index.tsx:246-267`), before the blanket
  `openExternal`, detect a file artifact whose `value`/`href` resolves to a
  local path. For those, route into the preview pane's file editor via the
  existing `PreviewTarget` mechanism.
- Keep `file://` remote-gateway and non-path artifacts on the current
  `downloadGatewayMediaFile` / `openExternal` path (R5).
- Do not change image/link artifact behavior; only file/text artifacts route to
  the editor. (Markdown detections stays as in `preview-file.tsx:1034` —
  `language === 'markdown'`.)

**Patterns to follow:**
- `src/app/chat/right-rail/preview-file.tsx` `isMarkdown`/`autoMode`
  (`preview-file.tsx:1034-1049`).
- `src/app/chat/right-rail/preview-artifact.tsx` — how an artifact opens as a
  preview `PreviewTarget`.

**Test scenarios:**
- A file artifact with a local `file:` path routes to the file editor.
- A `.md` artifact with a local path opens in the editor (rendered mode).
- A remote-gateway `file:` artifact still uses `downloadGatewayMediaFile`.
- An image/link artifact is unchanged.

**Verification:** Component test asserts the artifact `openArtifact` dispatches
to the preview target for local-path files and to the download/`openExternal`
path otherwise.

### U2. Confirm the editor save hotkey (`mod+s`) works from both modes

**Goal:** Verify `mod+s` saves the edited file from the file editor in local
and remote modes — no code change expected, since it already exists.

**Requirements:** R4 (verified, not implemented)

**Dependencies:** U1

**Files:**
- `apps/desktop/src/components/chat/code-editor.tsx` (reference only)
- `apps/desktop/src/app/chat/right-rail/preview-file.tsx` (reference only)

**Approach:**
- `CodeEditor` binds `Mod-s` → `save()` → `onSaveRef.current()` and
  `preview-file.tsx` passes `onSave={() => void saveEdit()}` to it, so
  `mod+s` already saves while the editor has focus. Confirm this path in both
  a local and a remote (`HERMES_DESKTOP_DEV_SERVER`/remote gateway) run; note
  it in the PR. No keybind is added because none is needed.

**Patterns to follow:**
- `code-editor.tsx` keymap (line ~251), `preview-file.tsx:965` `onSave`.

**Test scenarios:**
- `mod+s` while editing a dirty file triggers `saveEdit`, clears dirty.

**Verification:** Manual — open a file in the editor, edit, `mod+s`, confirm
the file on disk updates and the dirty indicator clears, in both modes.

### U3. Confirm theme/typography and polish

**Goal:** The editor from the Artifacts tab reads as deliberately Hermes-themed,
consistent with `DESIGN.md`.

**Requirements:** R2

**Dependencies:** U1

**Files:**
- `apps/desktop/src/app/chat/right-rail/preview-file.tsx` (already token-based;
  confirm only)

**Approach:**
- Since `preview-file.tsx` already uses `--ui-*` tokens and the shared
  markdown typography map, this unit is verification, not new CSS: confirm the
  markdown preview margins/type match the app, and that no new inline styles
  are introduced. Reuse `MarkdownPreview` as-is.

**Test scenarios:** n/a (visual); confirm no token regression via existing UI
tests/visual snapshots if the suite covers preview pages.

**Verification:** Manual/golden check that a `.md` artifact opens themed in both
light and dark modes.

---

## Scope Boundaries

### In Scope

- Routing `.md`/plain-text file artifacts from the Artifacts tab into the
  existing Hermes file editor.
- `mod+s` save hotkey scoped to the file editor.
- Tests for both.

### Out of Scope

- Building a new markdown renderer or editor (reuses existing).
- Changing the right-rail file editor's behavior when opened from a session
  beyond the new hotkey.
- Editing images/links/PDFs from the Artifacts tab.
- File-tree / multi-file browser or a full IDE — this is a single-file
  editor reachable from the Artifacts tab, per the request.

### Deferred to Follow-Up Work

- Editing artifacts held only in the registry (session-generated content with no
  on-disk path) — not recoverable to disk by design.
- A full file-tree pane. The Artifacts tab stays the entry point for now.

---

## Risks & Mitigations

- Risk: `mod+s` collides with a browser/OS default save. Mitigation: the
  handler is focus-guarded (only while editing), so outside the editor it is
  inert; confirm the browser default is preserved elsewhere.
- Risk: routing file artifacts into the preview could break the current "open in
  OS" expectation for users who want the system handler. Mitigation: keep
  image/link and non-local-path artifacts on the existing path; only local
  `.md`/text files route to the editor (R1/R5).
- Risk: dirty edits lost if the user switches artifacts mid-edit. Mitigation:
  reuse the existing `dirty`/`setPreviewDirty` state; decide and test whether
  switching a dirty file prompts before discard (matches existing file-editor
  behavior).

---

## Sources & Research

- `apps/desktop/src/app/artifacts/index.tsx` — Artifacts tab; `openArtifact`
  (`:246-267`), `RefreshCw` refresh (`:297-310`), `useRefreshHotkey` (`:154`).
- `apps/desktop/src/app/chat/right-rail/preview-file.tsx` — the existing
  file editor: `MarkdownPreview` (`:372`), `MARKDOWN_COMPONENTS` (`:358`),
  `PreviewModeSwitcher` (`:382`), `EditControls`/`saveEdit`/`writeDesktopFileText`
  (`:433-465`, `:904`), `isMarkdown`/`autoMode` (`:1034-1049`), keydown
  listener (`:857-860`).
- `apps/desktop/src/app/chat/right-rail/preview-artifact.tsx` — how an artifact
  opens as a preview target; `ArtifactPreview`.
- `apps/desktop/src/lib/keybinds/actions.ts` — keybind registry; `mod+s` free,
  `mod+shift+s` = `view.toggleStatusbar`.
- `apps/desktop/DESIGN.md` — visual contract.

---

## Verification Strategy

- `cd ~/.hermes/hermes-agent && node_modules/.bin/tsc -p apps/desktop/tsconfig.electron.json --noEmit` and the renderer tsconfig — both currently pass (EXIT=0), used as the baseline before/after.
- Run the focused test (U1 routing, U2 hotkey) in the chain that `npm test`/repo
  test scripts use.
- Manual: launch via `npm run dev` from `apps/desktop`, open a `.md` file
  artifact, confirm it opens in the editor, verify `mod+s` saves and the file
  on disk updates.