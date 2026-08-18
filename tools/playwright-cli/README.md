# Hermes Playwright CLI

Small local helper for capturing a page's accessibility snapshot and browser
console output during desktop/web QA.

## Usage

```bash
node tools/playwright-cli/capture.mjs http://127.0.0.1:5174 --name desktop-home
```

Options:

- `--out <dir>`: output directory, default `.playwright-cli`.
- `--name <label>`: safe label inserted in artifact filenames.
- `--headed`: open a visible browser.
- `--wait-ms <n>`: wait after DOMContentLoaded before capture.

## Policy

Commit this helper, not captured artifacts. `.playwright-cli/` is local scratch
state and should stay ignored because it may contain page text, console output,
paths, or user/session data.

For deterministic tests, add Playwright specs under the owning app instead of
checking in ad-hoc snapshots from this helper.
