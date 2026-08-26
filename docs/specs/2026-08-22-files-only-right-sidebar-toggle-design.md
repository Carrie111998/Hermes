# Files-Only Right Sidebar Toggle Design

**Date:** 2026-08-22
**Status:** Approved for specification review
**Scope:** Hermes Desktop renderer layout behavior

## Problem

Hermes Desktop currently treats the titlebar controls as positional edge toggles. The right control collapses the entire layout branch to the right of the main workspace. When a plugin pane such as Hermes Jarvis is docked immediately right of the conversation and the built-in Files pane is farther right, the control hides both panes.

The visible label, icon, and persisted state identify this as a file-browser control, but its behavior is broader than the file browser. This prevents users from hiding the file tree while keeping a persistent assistant pane visible.

## Desired experience

The default arrangement remains:

```text
Sessions | Main conversation | Jarvis | Files
```

Using the top-right Files control changes it to:

```text
Sessions | Main conversation | Jarvis
```

Jarvis stays immediately right of the conversation, remains mounted, retains its width and local state, and continues receiving live updates. Using the control again restores Files at the far right with its prior width.

## Goals

1. Make the titlebar right control toggle only the built-in Files pane.
2. Preserve all sibling plugin panes and their mounted state.
3. Keep the toolbar button, command, keybinding, and persisted file-browser state on one shared action path.
4. Restore Files to the same layout location and width after reopening.
5. Make the behavior generic for any plugin pane docked between the workspace and Files. The implementation must not contain Jarvis-specific logic.
6. Preserve pane flipping and existing left-sidebar behavior.

## Non-goals

- Changing Jarvis plugin registration or visual design.
- Adding a Jarvis-specific core integration.
- Redesigning the full pane tree or drag-and-drop system.
- Adding a second right-side titlebar control.
- Changing how the left Sessions sidebar toggle works.
- Installing or replacing the currently running Hermes Desktop application as part of implementation.

## Architecture

### Source of truth

The built-in Files pane remains identified by the existing layout-tree pane id `FILES_PANE_ID` and pane-state id `FILE_BROWSER_PANE_ID`. The renderer's existing pane tree remains authoritative for where Files is docked. The pane-state store remains authoritative for whether the file browser is open and for its width override.

### Files-only action

Replace the positional right-branch collapse behavior with a files-only action that:

1. Reads the current file-browser open state.
2. When open, closes or hides only the Files leaf in the layout tree and marks the file-browser pane state closed.
3. When closed, reveals the Files leaf using the existing tree reveal mechanism and marks the file-browser pane state open.
4. Preserves the layout records and width override needed for restoration.
5. Does not change the open state, dimensions, tab selection, or mount lifecycle of sibling panes.

The action belongs in the renderer layout store beside the existing file-browser state and tree helpers. Titlebar controls, palette commands, and keybindings call this single action rather than independently manipulating the layout.

### Pane flipping

Pane flipping changes physical placement but not semantic identity. The Files action always targets the built-in Files pane by id. It does not collapse whichever branch happens to occupy the physical right edge.

The left titlebar control remains unchanged. Any titlebar labels or comments that describe the Files action as a positional whole-side toggle must be updated to describe the files-only behavior.

## Interaction behavior

### Closing Files

- Clicking the top-right control while Files is visible hides only Files.
- Jarvis and other sibling plugin panes remain visible and mounted.
- Focus moves only if the currently focused element was inside Files. In that case, focus returns to the main workspace using the existing pane-shell focus behavior.
- The button tooltip and accessible label become `Show files` while Files is hidden and `Hide files` while Files is visible. These strings use the existing localization system.

### Reopening Files

- Clicking the same control reveals Files at its prior tree location.
- The previous file-browser width override is retained.
- The active file or preview tab remains governed by the existing preview store. This change does not clear it.
- Jarvis width and state remain unchanged.

### Missing or inconsistent Files state

If the built-in Files pane is temporarily absent from the tree, the action fails closed: it must not collapse a parent branch or hide sibling panes. It may reconcile the file-browser pane-state flag with actual tree visibility through existing layout helpers, but it must not manufacture a new arbitrary dock location.

## Persistence and compatibility

No new persistence key, configuration value, environment variable, plugin API, or backend contract is introduced.

Existing file-browser open state and width persistence remain in force. Existing user layouts migrate implicitly because the action targets the existing Files pane identity rather than changing the serialized tree format.

The change is compatible with third-party plugin panes because it removes broad branch collapse and targets only a built-in leaf. No plugin ids or assumptions about Jarvis appear in production code.

## Testing strategy

Implementation follows strict red-green-refactor TDD.

### Store-level behavior tests

Add focused tests that construct a layout with:

```text
workspace -> plugin pane -> files
```

Verify:

1. Closing Files changes only Files visibility.
2. The plugin pane remains visible and keeps its size/state records.
3. Reopening Files restores the Files leaf and prior width.
4. Repeated toggles are deterministic.
5. A missing Files leaf does not collapse or mutate sibling panes.
6. Flipped panes still target Files by semantic id.

### Titlebar integration tests

Verify:

1. The top-right button calls the files-only action.
2. The accessible label reflects Files visibility.
3. The left sidebar control retains its existing behavior.
4. No titlebar path calls the whole-right-branch collapse function for the Files action.

### Regression verification

Run at minimum:

```bash
npm run test:ui -- <focused layout and titlebar tests>
npm run typecheck
npm run lint
npm run test:ui
npm run build
```

Perform a disposable Desktop visual check with a plugin pane docked between the workspace and Files:

1. Open Files and confirm the four-pane arrangement.
2. Toggle Files off and confirm Jarvis remains visible and live.
3. Toggle Files on and confirm location and width restoration.
4. Flip pane sides and repeat.
5. Restart only the disposable verification instance and confirm persisted state restoration.

The installed Hermes application is not modified until the patch is reviewed and Drew explicitly approves deployment.

## Acceptance criteria

The change is complete only when:

- The Files control hides and shows only Files.
- Jarvis and arbitrary sibling plugin panes remain visible, mounted, and unchanged.
- File-browser location, width, and relevant preview state restore correctly.
- Missing Files state cannot trigger broader pane collapse.
- Pane flipping works by semantic Files identity.
- Focus remains usable and accessible labels remain accurate.
- Focused tests, full Desktop UI tests, typecheck, lint, build, and disposable visual verification pass with fresh evidence.
