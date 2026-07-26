/**
 * E2E coverage for the recent-projects switcher (⌘K → "Switch project…").
 *
 * These tests drive the REAL app: electron → `hermes serve` → renderer, with a
 * mock inference server standing in for the LLM. Everything the switcher
 * touches is real — the command palette, the cmdk overlay, the existence probe
 * (`hermes:fs:readDir` IPC against actual directories), `session.cwd.set` /
 * `config.get` against the live gateway, and the statusbar that renders the
 * resulting workspace.
 *
 * ONE thing is stubbed, and it is deliberately not the thing under test: the
 * native folder dialog. `pickProjectFolder()` bottoms out in
 * `dialog.showOpenDialog` in the main process, which Playwright cannot click
 * (it's an OS-owned window). We patch `dialog.showOpenDialog` via
 * `electronApp.evaluate` so it resolves to a temp fixture directory. The
 * renderer path under test — the palette entry, the overlay, the probe, the
 * cwd mutation, the MRU write — all still runs for real; only the OS file
 * chooser's return value is injected.
 *
 * Prerequisite: `npm run build` must have been run so dist/ exists.
 */

import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'
import { type ElectronApplication, expect, type Page, test } from './test'

let fixture: MockBackendFixture | null = null

/** Temp fixture workspaces, created on disk so the existence probe is real. */
let projectAlpha = ''
let projectBeta = ''
/** Recorded, then deleted from disk, to exercise the missing-directory guard. */
let projectGhost = ''

/** Where the switcher screenshots land, reported back to the user. */
const SCREENSHOT_DIR = path.join(os.tmpdir(), 'hermes-project-switcher-e2e-shots')

function makeProjectDir(name: string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), `hermes-e2e-project-${name}-`))

  // A file inside makes the folder look like a real workspace and keeps
  // `readDir` returning entries rather than an empty listing.
  fs.writeFileSync(path.join(dir, 'README.md'), `# ${name}\n`, 'utf8')

  return fs.realpathSync(dir)
}

function removeTempDir(dir: string): void {
  // Guard: only ever delete inside the OS temp dir, and only our own prefix.
  const tmpRoot = fs.realpathSync(os.tmpdir())

  if (!dir || !dir.startsWith(tmpRoot) || !path.basename(dir).startsWith('hermes-e2e-project-')) {
    return
  }

  fs.rmSync(dir, { force: true, recursive: true })
}

/**
 * Stub the OS folder chooser so "Open folder…" resolves to `dir`.
 *
 * Returns nothing; the patch stays installed for the rest of the run and is
 * re-pointed by calling this again. See the file header for why this is the
 * one acceptable stub.
 */
async function stubFolderDialog(app: ElectronApplication, dir: string): Promise<void> {
  await app.evaluate(async ({ dialog }, targetDir) => {
    const patched = dialog as unknown as { __hermesE2EOriginalShowOpenDialog?: unknown }

    if (!patched.__hermesE2EOriginalShowOpenDialog) {
      patched.__hermesE2EOriginalShowOpenDialog = dialog.showOpenDialog
    }

    // Cover both overloads (with and without a parent BrowserWindow) — the
    // renderer's selectPaths handler calls the windowed form.
    dialog.showOpenDialog = (async () => ({ canceled: false, filePaths: [targetDir] })) as typeof dialog.showOpenDialog
  }, dir)
}

/** Read the persisted MRU exactly as the renderer stores it. */
async function readRecentProjects(page: Page): Promise<Array<{ openedAt: number; path: string }>> {
  return page.evaluate(() => {
    const raw = window.localStorage.getItem('hermes.desktop.recentProjects')

    return raw ? (JSON.parse(raw) as Array<{ openedAt: number; path: string }>) : []
  })
}

/**
 * Labels of every statusbar item. The workspace item renders the cwd's leaf
 * (see `workspaceLabel` in use-statusbar-items), which is what the user
 * actually sees telling them which project they're in. Each fixture dir has a
 * unique `mkdtemp` suffix, so the leaf identifies the project unambiguously.
 */
async function statusbarLabels(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const statusbar = document.querySelector('[data-slot="statusbar"]')

    if (!statusbar) {
      return []
    }

    return [...statusbar.querySelectorAll('button')].map(button => (button.textContent ?? '').trim())
  })
}

/**
 * `/var/...` and `/private/var/...` are the same directory on macOS; the
 * backend may echo either. Compare on one form so the assertion is about the
 * path, not the alias.
 */
const canonical = (value: string): string => value.replace(/^\/private\//, '/')

/**
 * The full cwd the app is anchored at, read from the statusbar workspace
 * item's tooltip (`title` in use-statusbar-items → a Radix tooltip, not a DOM
 * title attribute). This is the whole path, so it proves the app followed the
 * switch rather than merely showing a same-named folder.
 */
async function hoveredWorkspacePath(page: Page, projectDir: string): Promise<string> {
  await page
    .locator('[data-slot="statusbar"] button')
    .filter({ hasText: path.basename(projectDir) })
    .hover()

  const tooltip = page.getByRole('tooltip').filter({ hasText: path.basename(projectDir) }).first()
  await expect(tooltip).toBeVisible({ timeout: 10_000 })

  return canonical((await tooltip.textContent())?.trim() ?? '')
}

/** Open the command palette the way a user does: the ⌘K / Ctrl+K keybind. */
async function openCommandPalette(page: Page): Promise<void> {
  await page.keyboard.press(process.platform === 'darwin' ? 'Meta+k' : 'Control+k')
  await expect(page.locator('[data-slot="command-input"]')).toBeVisible({ timeout: 10_000 })
}

/** The switcher overlay, identified by its own search placeholder. */
function switcherInput(page: Page) {
  return page.locator('[data-slot="command-input"][placeholder="Search recent projects…"]')
}

/** Reach the switcher through the palette, as a user would discover it. */
async function openSwitcherViaPalette(page: Page): Promise<void> {
  await openCommandPalette(page)

  const paletteInput = page.locator('[data-slot="command-input"]').first()
  await paletteInput.fill('switch project')

  const entry = page.getByRole('option', { name: /Switch project/ }).first()
  await expect(entry).toBeVisible({ timeout: 10_000 })
  await entry.click()

  await expect(switcherInput(page)).toBeVisible({ timeout: 10_000 })
}

async function closeSwitcher(page: Page): Promise<void> {
  await page.keyboard.press('Escape')
  await expect(switcherInput(page)).toHaveCount(0, { timeout: 10_000 })
}

/** Row locator for a project, matched on the path line the switcher renders. */
function switcherRow(page: Page, projectDir: string) {
  return page.getByRole('option').filter({ hasText: path.basename(projectDir) })
}

test.beforeAll(async () => {
  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true })

  projectAlpha = makeProjectDir('alpha')
  projectBeta = makeProjectDir('beta')
  projectGhost = makeProjectDir('ghost')

  fixture = await setupMockBackend()
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null

  removeTempDir(projectAlpha)
  removeTempDir(projectBeta)
  removeTempDir(projectGhost)
})

test.describe('project switcher', () => {
  test('is reachable from the command palette and shows an empty state', async () => {
    const page = fixture!.page

    // Criterion 1 — discoverability: the palette entry exists and opens it.
    await openSwitcherViaPalette(page)

    // Criterion 2 — empty MRU: sensible empty state + a browse affordance.
    await expect(page.getByText('No recent projects yet.')).toBeVisible()
    await expect(page.getByRole('option', { name: /Open folder/ })).toBeVisible()

    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'switcher-empty.png') })

    await closeSwitcher(page)
  })

  test('records a browsed folder and anchors the workspace at it', async () => {
    const page = fixture!.page

    // The native chooser is OS-owned; inject its answer (see file header).
    await stubFolderDialog(fixture!.app, projectAlpha)

    await openSwitcherViaPalette(page)
    await page.getByRole('option', { name: /Open folder/ }).click()

    // Criterion 4 (first half) — the app reflects the new cwd, as reported by
    // the statusbar workspace item (fed by $currentCwd after the gateway
    // round trip), not merely by the list having changed.
    await expect.poll(() => statusbarLabels(page), { timeout: 30_000 }).toContain(path.basename(projectAlpha))

    // …and it's the whole path, not just a folder with the same name.
    expect(await hoveredWorkspacePath(page, projectAlpha)).toBe(canonical(projectAlpha))

    // Criterion 3 — the switch is recorded in the MRU.
    await expect.poll(() => readRecentProjects(page), { timeout: 10_000 }).toEqual([
      expect.objectContaining({ path: projectAlpha })
    ])

    // …and shows up when the switcher is reopened.
    await openSwitcherViaPalette(page)
    await expect(switcherRow(page, projectAlpha)).toBeVisible()
    await expect(page.getByText('No recent projects yet.')).toHaveCount(0)

    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'switcher-with-recents.png') })

    await closeSwitcher(page)
  })

  test('switching between two projects moves the working directory', async () => {
    const page = fixture!.page

    await stubFolderDialog(fixture!.app, projectBeta)

    await openSwitcherViaPalette(page)
    await page.getByRole('option', { name: /Open folder/ }).click()

    // Criterion 4 — the second project actually becomes the workspace.
    await expect.poll(() => statusbarLabels(page), { timeout: 30_000 }).toContain(path.basename(projectBeta))
    expect(await hoveredWorkspacePath(page, projectBeta)).toBe(canonical(projectBeta))

    // Criterion 5 — recency: the just-opened project sorts first.
    await expect.poll(() => readRecentProjects(page), { timeout: 10_000 }).toEqual([
      expect.objectContaining({ path: projectBeta }),
      expect.objectContaining({ path: projectAlpha })
    ])

    // Now switch BACK to alpha from the list itself (not the folder dialog):
    // this is the pure recent-entry path, with nothing stubbed.
    await openSwitcherViaPalette(page)

    const rows = await page.getByRole('option').allTextContents()
    // The MRU order must be visible in the rendered list, not just in storage.
    const betaIndex = rows.findIndex(text => text.includes(path.basename(projectBeta)))
    const alphaIndex = rows.findIndex(text => text.includes(path.basename(projectAlpha)))
    expect(betaIndex).toBeGreaterThanOrEqual(0)
    expect(alphaIndex).toBeGreaterThan(betaIndex)

    await switcherRow(page, projectAlpha).click()

    await expect.poll(() => statusbarLabels(page), { timeout: 30_000 }).toContain(path.basename(projectAlpha))
    expect(await hoveredWorkspacePath(page, projectAlpha)).toBe(canonical(projectAlpha))

    await expect.poll(() => readRecentProjects(page), { timeout: 10_000 }).toEqual([
      expect.objectContaining({ path: projectAlpha }),
      expect.objectContaining({ path: projectBeta })
    ])
  })

  test('marks a deleted project missing and refuses to switch to it', async () => {
    const page = fixture!.page

    // Record the ghost project through the real switch path while it exists…
    await stubFolderDialog(fixture!.app, projectGhost)

    await openSwitcherViaPalette(page)
    await page.getByRole('option', { name: /Open folder/ }).click()

    await expect.poll(() => statusbarLabels(page), { timeout: 30_000 }).toContain(path.basename(projectGhost))

    // …then move the workspace off it and delete it from disk, the way a
    // renamed/unmounted/removed project behaves between visits.
    await openSwitcherViaPalette(page)
    await switcherRow(page, projectAlpha).click()
    await expect.poll(() => statusbarLabels(page), { timeout: 30_000 }).toContain(path.basename(projectAlpha))

    removeTempDir(projectGhost)

    // Criterion 6 — the row is shown as missing (the switcher re-probes on
    // every open) and selecting it must NOT re-anchor the workspace.
    await openSwitcherViaPalette(page)

    const ghostRow = switcherRow(page, projectGhost)
    await expect(ghostRow).toContainText('Missing', { timeout: 30_000 })
    await expect(ghostRow).toContainText('This folder is no longer available.')

    await page.screenshot({ path: path.join(SCREENSHOT_DIR, 'switcher-missing-project.png') })

    await ghostRow.click()

    // The dead path is refused: the overlay stays open and the workspace is
    // still the live project, so the agent's tools never follow it.
    await expect(switcherInput(page)).toBeVisible()

    await closeSwitcher(page)
    expect(await statusbarLabels(page)).toContain(path.basename(projectAlpha))
    expect(await statusbarLabels(page)).not.toContain(path.basename(projectGhost))
    expect(await hoveredWorkspacePath(page, projectAlpha)).toBe(canonical(projectAlpha))
  })
})
