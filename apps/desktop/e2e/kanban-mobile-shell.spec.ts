import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import * as fs from 'node:fs'
import * as path from 'node:path'

import { type MockBackendFixture, setupMockBackend } from './fixtures'
import { expect, test } from './test'

const REPO_ROOT = path.resolve(import.meta.dirname, '..', '..', '..')
const COLUMNS = ['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done'] as const
const MOBILE_WIDTHS = [320, 360, 390, 430] as const
const LONG_TITLE = 'Synthetic review card with an intentionally long unbroken-token-safe title for drawer and clipping validation'

function runBoardScript(hermesHome: string, source: string) {
  const result = spawnSync(process.env.PYTHON ?? 'python', ['-c', source], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    env: { ...process.env, HERMES_HOME: hermesHome, PYTHONPATH: REPO_ROOT }
  })

  if (result.status !== 0) {throw new Error(`Kanban fixture failed: ${result.stderr}`)}
}

function seedBoard(hermesHome: string) {
  runBoardScript(hermesHome, `
from hermes_cli import kanban_db
kanban_db.init_db()
conn = kanban_db.connect()
conn.execute("DELETE FROM task_events")
conn.execute("DELETE FROM tasks")
def add(title, status, assignee, priority):
    task_id = kanban_db.create_task(conn, title=title, body=("Synthetic fixture only. " * 20), assignee=assignee, created_by="desktop-e2e", priority=priority, initial_status="running")
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
for index, status in enumerate(${JSON.stringify(COLUMNS.filter(status => status !== 'scheduled'))}):
    add(f"Synthetic {status} card {index}", status, "fixture-alpha" if index % 2 == 0 else "fixture-beta", 90-index)
for index in range(4):
    add(f"Synthetic long triage card {index} with deterministic wrapping words", "triage", "fixture-alpha", 70-index)
add(${JSON.stringify(LONG_TITLE)}, "review", "fixture-beta", 65)
add("Synthetic second running profile card", "running", "fixture-beta", 60)
archived = kanban_db.create_task(conn, title="Synthetic archived status-filter card", body="Synthetic fixture only.", assignee="fixture-alpha", created_by="desktop-e2e", priority=1, initial_status="running")
conn.execute("UPDATE tasks SET status = 'archived' WHERE id = ?", (archived,))
conn.commit()
conn.close()
`)
}

async function setContentViewport(fixture: MockBackendFixture, width: number) {
  await fixture.app.evaluate(({ BrowserWindow }, size) => {
    const window = BrowserWindow.getAllWindows()[0]
    window?.setMinimumSize(0, 0)
    window?.setContentSize(size, 760)
  }, width)
  await expect.poll(() => fixture.page.evaluate(() => window.innerWidth)).toBe(width)
}

async function expectContained(page: MockBackendFixture['page'], status: string) {
  const selector = page.locator(`[data-kanban-lane-selector="${status}"]`)
  const lane = page.locator(`#kanban-lane-${status}`)
  await page.waitForTimeout(100)
  await expect(selector).toHaveAttribute('aria-controls', `kanban-lane-${status}`)
  await expect(selector).toHaveAttribute('aria-selected', 'true')
  await expect(selector).toHaveAttribute('tabindex', '0')
  await expect(selector).toBeFocused()

  const geometry = await lane.evaluate(element => {
    const scroller = element.parentElement!
    const laneRect = element.getBoundingClientRect()
    const scrollRect = scroller.getBoundingClientRect()

    return {
      documentOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      laneLeft: laneRect.left,
      laneRight: laneRect.right,
      scrollerOverflow: scroller.scrollWidth > scroller.clientWidth,
      scrollLeft: scrollRect.left,
      scrollRight: scrollRect.right
    }
  })
  expect(geometry.documentOverflow).toBe(0)
  expect(geometry.scrollerOverflow).toBe(true)
  expect(geometry.laneLeft).toBeGreaterThanOrEqual(geometry.scrollLeft - 1)
  expect(geometry.laneRight).toBeLessThanOrEqual(geometry.scrollRight + 1)
}

async function expectMobileTargetsAndClipping(page: MockBackendFixture['page']) {
  for (const status of COLUMNS) {
    const box = await page.locator(`[data-kanban-lane-selector="${status}"]`).boundingBox()
    expect(box?.width).toBe(44)
    expect(box?.height).toBe(44)
  }

  const clipping = await page.locator('[data-kanban-board-root]:visible').evaluate(root => {
    const cards = [...root.querySelectorAll<HTMLElement>('[data-kanban-card]')]
    const labels = [...root.querySelectorAll<HTMLElement>('[data-kanban-lane] header span')]

    return {
      cardClipping: cards.some(card => card.scrollWidth > card.clientWidth + 1),
      labelMidwordSplits: labels.some(label => getComputedStyle(label).overflowWrap === 'anywhere'),
      rootClipping: root.scrollWidth > root.clientWidth + 1
    }
  })
  expect(clipping).toEqual({ cardClipping: false, labelMidwordSplits: false, rootClipping: false })
}

let fixture: MockBackendFixture | null = null

test.describe.configure({ mode: 'serial', timeout: 180_000 })
test.use({ trace: 'off' })

test.beforeAll(async () => {
  fixture = await setupMockBackend({
    extraConfig: 'plugins:\n  enabled:\n    - kanban',
    prepareSandbox: sandbox => seedBoard(sandbox.hermesHome)
  })
  await fixture.page.waitForSelector('body')
  await fixture.page.evaluate(() => {
    localStorage.setItem('hermes.desktop.pluginDecisions.v2', JSON.stringify({ kanban: true }))
  })
  await fixture.page.reload()
  const nav = fixture.page.getByRole('button', { name: 'Kanban', exact: true })
  await expect(nav).toBeVisible({ timeout: 60_000 })
  await nav.click()
  await expect(fixture.page.locator('[data-kanban-lane="triage"]')).toBeVisible()
})

test.afterAll(async () => {
  fixture?.app.process().kill()
  await fixture?.mock.close()
  fixture?.sandbox.cleanup()
  fixture = null
})

// Playwright requires object destructuring even though this spec owns its Electron fixture.
// eslint-disable-next-line no-empty-pattern
test('actual shell validates exact mobile viewports and all lane input paths', async ({}, testInfo) => {
  const page = fixture!.page
  const manifest: Array<Record<string, number | string>> = []

  for (const width of MOBILE_WIDTHS) {
    await setContentViewport(fixture!, width)
    const shell = await page.evaluate(() => ({
      board: document.querySelector<HTMLElement>('[data-kanban-board-root]')!.clientWidth,
      lane: document.querySelector<HTMLElement>('[data-kanban-lane="triage"]')!.clientWidth,
      viewport: innerWidth
    }))
    expect(shell.viewport).toBe(width)
    expect(shell.board).toBe(shell.viewport) // the mobile shell hides its desktop rail; no board delta
    expect(shell.lane).toBe(shell.board - 32) // the board's explicit 2rem horizontal lane gutter
    await expectMobileTargetsAndClipping(page)

    for (const status of COLUMNS) {
      const selector = page.locator(`[data-kanban-lane-selector="${status}"]`)
      await selector.click()
      await selector.focus()
      await expectContained(page, status)
    }

    const first = page.locator('[data-kanban-lane-selector="triage"]')
    await first.focus()
    await first.press('End')
    await expectContained(page, 'done')
    await page.locator('[data-kanban-lane-selector="done"]').press('ArrowRight')
    await expectContained(page, 'done') // explicit non-wrapping edge policy
    await page.locator('[data-kanban-lane-selector="done"]').press('Home')
    await expectContained(page, 'triage')
    await first.press('ArrowLeft')
    await expectContained(page, 'triage')
    for (const status of COLUMNS.slice(1)) {
      const current = page.locator('[role="tab"][aria-selected="true"]')
      await current.press('ArrowRight')
      await expectContained(page, status)
    }
    for (const status of [...COLUMNS].reverse().slice(1)) {
      const current = page.locator('[role="tab"][aria-selected="true"]')
      await current.press('ArrowLeft')
      await expectContained(page, status)
    }

    const bytes = await page.screenshot({ fullPage: true })
    const hash = createHash('sha256').update(bytes).digest('hex')
    const filename = `kanban-mobile-viewport-${width}.png`
    const outputPath = testInfo.outputPath(filename)
    fs.writeFileSync(outputPath, bytes)
    await testInfo.attach(filename, { path: outputPath, contentType: 'image/png' })
    manifest.push({ boardWidth: shell.board, bytes: bytes.length, filename, sha256: hash, viewportWidth: shell.viewport })
  }

  const manifestPath = testInfo.outputPath('kanban-mobile-viewport-manifest.json')
  fs.writeFileSync(manifestPath, `${JSON.stringify(manifest, null, 2)}\n`)
  await testInfo.attach('kanban-mobile-viewport-manifest.json', { path: manifestPath, contentType: 'application/json' })
})

// eslint-disable-next-line no-empty-pattern
test('mobile fixture covers long cards, drawer actions, filters, grouping, and empty states', async ({}) => {
  const page = fixture!.page
  await setContentViewport(fixture!, 390)

  await page.locator('[data-kanban-lane-selector="review"]').click()
  await page.getByText(LONG_TITLE, { exact: true }).last().click()
  const drawer = page.locator('[data-kanban-layout="mobile"]').last()
  await expect(drawer).toBeVisible()
  const actions = page.getByRole('button', { name: 'Task actions' })
  const close = page.getByRole('button', { name: 'Close' })
  for (const control of [actions, close]) {
    const box = await control.boundingBox()
    expect(box?.width).toBe(44)
    expect(box?.height).toBe(44)
  }
  const drawerGeometry = await drawer.evaluate(element => ({ client: element.clientWidth, scroll: element.scrollWidth, viewport: innerWidth }))
  expect(drawerGeometry).toEqual({ client: 389, scroll: 389, viewport: 390 }) // 1px shell border, no content clipping
  await actions.click()
  await expect(page.getByText('Copy task id', { exact: true })).toBeVisible()
  await page.keyboard.press('Escape')
  await expect(actions).toBeHidden()

  const search = page.getByRole('textbox', { name: 'Filter cards…' })
  await search.fill('Synthetic long triage')
  await expect(search).toHaveValue('Synthetic long triage')
  await expect(page.getByText('Synthetic long triage card 0 with deterministic wrapping words', { exact: true }).first()).toBeVisible()
  await expectMobileTargetsAndClipping(page)
  await search.fill('definitely-no-synthetic-match')
  await expect(page.getByText('No tasks match the filters', { exact: true })).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0)
  await search.fill('')

  const filters = page.getByRole('button', { name: 'Filters' })
  await filters.click()
  await page.getByText('Show archived', { exact: true }).click()
  await expect(page.getByText('Synthetic archived status-filter card', { exact: true }).first()).toBeVisible()
  await filters.click()
  await page.getByText('Group Running by profile', { exact: true }).click()
  await page.locator('[data-kanban-lane-selector="running"]').click()
  await expect(page.getByText('fixture-alpha', { exact: true }).first()).toBeVisible()
  await expect(page.getByText('fixture-beta', { exact: true }).first()).toBeVisible()
  await filters.click()
  await page.getByText('Group Running by profile', { exact: true }).click()

  await page.locator('[data-kanban-lane-selector="scheduled"]').click()
  await expect(page.locator('[data-kanban-lane="scheduled"]')).toHaveAttribute('aria-label', 'Expand Scheduled')

  runBoardScript(fixture!.sandbox.hermesHome, `
from hermes_cli import kanban_db
conn = kanban_db.connect()
conn.execute("DELETE FROM task_events")
conn.execute("DELETE FROM tasks")
conn.commit()
conn.close()
`)
  await page.reload()
  await expect(page.getByText('No tasks on this board', { exact: true })).toBeVisible({ timeout: 30_000 })
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0)
})

// eslint-disable-next-line no-empty-pattern
test('1280 desktop regression keeps lanes, navigation, and drawer geometry', async ({}) => {
  const page = fixture!.page
  seedBoard(fixture!.sandbox.hermesHome)
  await page.reload()
  await setContentViewport(fixture!, 1280)
  await expect(page.locator('[data-kanban-layout="desktop"]').first()).toBeVisible()
  await expect(page.locator('[data-kanban-lane-selector]')).toHaveCount(8)
  await expect(page.locator('[data-kanban-lane-selector]').first()).toBeHidden()
  const widths = await page.locator('[data-kanban-lane]').evaluateAll(lanes => lanes.map(lane => lane.getBoundingClientRect().width))
  expect(widths.filter(width => width === 256).length).toBeGreaterThanOrEqual(7)
  expect(widths).toContain(32)
  await page.getByText(LONG_TITLE, { exact: true }).last().click()
  const drawer = page.locator('[data-kanban-layout="desktop"]').last()
  await expect(drawer).toHaveJSProperty('clientWidth', 415) // 26rem outer width minus the 1px shell border
  const actionBox = await page.getByRole('button', { name: 'Task actions' }).boundingBox()
  expect(actionBox?.width).toBe(24)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0)
})
