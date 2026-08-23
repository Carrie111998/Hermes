import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import * as fs from 'node:fs'
import * as os from 'node:os'
import * as path from 'node:path'

import { type MockBackendFixture, setupMockBackend } from './fixtures'
import { expect, test } from './test'

const REPO_ROOT = path.resolve(import.meta.dirname, '..', '..', '..')
const COLUMNS = ['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done'] as const
const MOBILE_WIDTHS = [320, 360, 390, 430] as const
const LONG_TITLE = 'Synthetic review card with an intentionally long unbroken-token-safe title for drawer and clipping validation'

function runBoardScript(hermesHome: string, source: string) {
  const env: Record<string, string | undefined> = { ...process.env, HERMES_HOME: hermesHome, PYTHONPATH: REPO_ROOT }
  delete env.HERMES_KANBAN_BOARD
  delete env.HERMES_KANBAN_DB
  delete env.HERMES_KANBAN_TASK

  const result = spawnSync(process.env.PYTHON ?? 'python', ['-c', source], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    env
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

interface BoardIdentity {
  events: Array<[number, string, string]>
  tasks: Array<[string, string]>
}

function readBoardIdentity(hermesHome: string, kanbanDb?: string): BoardIdentity {
  const env: Record<string, string | undefined> = {
    ...process.env,
    HERMES_HOME: hermesHome,
    HERMES_KANBAN_DB: kanbanDb,
    PYTHONPATH: REPO_ROOT,
  }
  delete env.HERMES_KANBAN_BOARD
  delete env.HERMES_KANBAN_TASK
  const result = spawnSync(process.env.PYTHON ?? 'python', ['-c', `
import json
from hermes_cli import kanban_db
conn = kanban_db.connect()
print(json.dumps({
    "events": [list(row) for row in conn.execute("SELECT id, task_id, kind FROM task_events ORDER BY id")],
    "tasks": [list(row) for row in conn.execute("SELECT id, title FROM tasks ORDER BY id")],
}, sort_keys=True))
conn.close()
`], { cwd: REPO_ROOT, encoding: 'utf8', env })
  if (result.status !== 0) {
    throw new Error(`Kanban identity read failed: ${result.stderr}`)
  }
  return JSON.parse(result.stdout) as BoardIdentity
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
  await expect(selector).toHaveAttribute('aria-current', 'page')
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

async function measureTrailingPeek(page: MockBackendFixture['page'], current: string, next?: string) {
  return page.locator('[data-kanban-scroller]:visible').evaluate((scroller, lanes) => {
    const currentElement = scroller.querySelector<HTMLElement>(`[data-kanban-lane="${lanes.current}"]`)!
    const nextElement = lanes.next
      ? scroller.querySelector<HTMLElement>(`[data-kanban-lane="${lanes.next}"]`)
      : null
    const viewportLeft = 0
    const viewportRight = innerWidth
    const currentRect = currentElement.getBoundingClientRect()
    const nextRect = nextElement?.getBoundingClientRect()

    return {
      currentContained: currentRect.left >= viewportLeft && currentRect.right <= viewportRight,
      currentSquare: getComputedStyle(currentElement).borderRadius === '0px',
      nextBorder: nextElement ? getComputedStyle(nextElement).borderLeftWidth : null,
      trailingPeek: nextRect ? Math.max(0, viewportRight - nextRect.left) : 0,
      remaining: Math.round(scroller.scrollWidth - scroller.clientWidth - scroller.scrollLeft),
    }
  }, { current, next })
}

let fixture: MockBackendFixture | null = null
let canonicalRoot: string | null = null
let canonicalDb = ''
let canonicalDbLink = ''
let canonicalBytes: Buffer
let canonicalIdentity: BoardIdentity

test.describe.configure({ mode: 'serial', timeout: 180_000 })
test.use({ trace: 'off' })

test.beforeAll(async () => {
  canonicalRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-e2e-canonical-shell-'))
  const canonicalHome = path.join(canonicalRoot, 'home')
  canonicalDb = path.join(canonicalRoot, 'sentinel.db')
  canonicalDbLink = path.join(canonicalRoot, 'sentinel-link.db')
  fs.mkdirSync(canonicalHome)
  runBoardScript(canonicalHome, `
from hermes_cli import kanban_db
kanban_db.init_db()
conn = kanban_db.connect()
kanban_db.create_task(conn, title="canonical spawned-app sentinel", body="must remain immutable", created_by="desktop-e2e-sentinel")
conn.commit()
conn.close()
`)
  fs.renameSync(path.join(canonicalHome, 'kanban.db'), canonicalDb)
  fs.symlinkSync(canonicalDb, canonicalDbLink)
  canonicalIdentity = readBoardIdentity(canonicalHome, canonicalDb)
  canonicalBytes = fs.readFileSync(canonicalDb)

  fixture = await setupMockBackend({
    extraConfig: 'plugins:\n  enabled:\n    - kanban',
    // The actual Electron/backend launch must remain pinned to this fixture's
    // HERMES_HOME even when a caller supplies ambient board selectors.
    appEnv: {
      HERMES_HOME: canonicalHome,
      HERMES_KANBAN_DB: canonicalDbLink,
      HERMES_KANBAN_BOARD: 'hostile-canonical-board',
    },
    prepareSandbox: sandbox => seedBoard(sandbox.hermesHome)
  })
  await fixture.page.emulateMedia({ reducedMotion: 'reduce' })
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
  if (canonicalRoot) {
    fs.rmSync(canonicalRoot, { recursive: true, force: true })
  }
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
      overscrollBehaviorX: getComputedStyle(document.querySelector<HTMLElement>('[data-kanban-scroller]')!).overscrollBehaviorX,
      viewport: innerWidth
    }))
    expect(shell.viewport).toBe(width)
    expect(shell.board).toBe(shell.viewport) // the mobile shell hides its desktop rail; no board delta
    expect(shell.lane).toBe(shell.board - 38) // 36px outer gutter plus the lane's two one-pixel borders
    expect(shell.overscrollBehaviorX).toBe('contain')
    await expectMobileTargetsAndClipping(page)

    const scroller = page.locator('[data-kanban-scroller]:visible')
    const card = page.locator('[data-kanban-card]').first()
    await scroller.dispatchEvent('mousedown', { button: 0, clientX: 200, clientY: 500 })
    await page.mouse.move(150, 500)
    await expect(scroller).toHaveCSS('scroll-snap-type', 'none')
    await page.mouse.up()
    await expect(scroller).toHaveCSS('scroll-snap-type', 'x mandatory')
    await card.dispatchEvent('dragstart', { dataTransfer: await page.evaluateHandle(() => new DataTransfer()) })
    await expect(scroller).toHaveCSS('scroll-snap-type', 'none')
    await card.dispatchEvent('dragend')
    await expect(scroller).toHaveCSS('scroll-snap-type', 'x mandatory')
    await scroller.evaluate(element => element.scrollTo({ left: 0, behavior: 'instant' }))
    await expect.poll(() => measureTrailingPeek(page, 'triage', 'todo')).toMatchObject({
      currentContained: true,
      currentSquare: true,
      nextBorder: '1px',
      trailingPeek: 12,
    })
    const firstBytes = await page.screenshot({ fullPage: true })
    const firstFilename = `kanban-mobile-${width}-before-scroll.png`
    const firstPath = testInfo.outputPath(firstFilename)
    fs.writeFileSync(firstPath, firstBytes)
    await testInfo.attach(firstFilename, { path: firstPath, contentType: 'image/png' })
    manifest.push({ bytes: firstBytes.length, filename: firstFilename, sha256: createHash('sha256').update(firstBytes).digest('hex'), state: 'first', viewportWidth: width })

    // Real wheel input exercises the pointer/touchpad-equivalent path. Snap
    // settling must expose the next square lane's one-pixel leading border.
    const scrollerBox = await scroller.boundingBox()
    await page.mouse.move(scrollerBox!.x + scrollerBox!.width / 2, scrollerBox!.y + 20)
    await page.mouse.wheel((shell.lane + 10) * 2 + 40, 0)
    await expect.poll(() => measureTrailingPeek(page, 'ready', 'running')).toMatchObject({
      currentContained: true,
      currentSquare: true,
      nextBorder: '1px',
      trailingPeek: 12,
    })
    await expect(page.locator('[data-kanban-lane-selector="ready"]')).toHaveAttribute('aria-current', 'page')
    const middleBytes = await page.screenshot({ fullPage: true })
    const middleFilename = `kanban-mobile-${width}-after-pointer-scroll.png`
    const middlePath = testInfo.outputPath(middleFilename)
    fs.writeFileSync(middlePath, middleBytes)
    await testInfo.attach(middleFilename, { path: middlePath, contentType: 'image/png' })
    manifest.push({ bytes: middleBytes.length, filename: middleFilename, sha256: createHash('sha256').update(middleBytes).digest('hex'), state: 'middle', viewportWidth: width })

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
    await expect.poll(() => measureTrailingPeek(page, 'done')).toMatchObject({
      currentContained: true,
      currentSquare: true,
      remaining: 0,
      trailingPeek: 0,
    })
    const endBytes = await page.screenshot({ fullPage: true })
    const endFilename = `kanban-mobile-${width}-end-no-hint.png`
    const endPath = testInfo.outputPath(endFilename)
    fs.writeFileSync(endPath, endBytes)
    await testInfo.attach(endFilename, { path: endPath, contentType: 'image/png' })
    manifest.push({ bytes: endBytes.length, filename: endFilename, sha256: createHash('sha256').update(endBytes).digest('hex'), state: 'end', viewportWidth: width })
    await page.locator('[data-kanban-lane-selector="done"]').press('ArrowRight')
    await expectContained(page, 'done') // explicit non-wrapping edge policy
    await page.locator('[data-kanban-lane-selector="done"]').press('Home')
    await expectContained(page, 'triage')
    await first.press('ArrowLeft')
    await expectContained(page, 'triage')
    for (const status of COLUMNS.slice(1)) {
      const current = page.locator('[data-kanban-lane-selector][aria-current="page"]')
      await current.press('ArrowRight')
      await expectContained(page, status)
    }
    for (const status of [...COLUMNS].reverse().slice(1)) {
      const current = page.locator('[data-kanban-lane-selector][aria-current="page"]')
      await current.press('ArrowLeft')
      await expectContained(page, status)
    }

    const reducedMotion = await page.evaluate(() => ({
      matches: matchMedia('(prefers-reduced-motion: reduce)').matches,
      scrollBehavior: getComputedStyle(document.querySelector<HTMLElement>('[data-kanban-scroller]')!).scrollBehavior,
      transitionDuration: getComputedStyle(document.querySelector<HTMLElement>('[data-kanban-lane="triage"]')!).transitionDuration,
    }))
    expect(reducedMotion.matches).toBe(true)
    expect(reducedMotion.scrollBehavior).toBe('auto')
    expect(Number.parseFloat(reducedMotion.transitionDuration)).toBeLessThanOrEqual(0.001)
  }

  const manifestPath = testInfo.outputPath('kanban-mobile-viewport-manifest.json')
  const distRoot = path.join(REPO_ROOT, 'apps', 'desktop', 'dist')
  const rendererBundle = fs.readdirSync(path.join(distRoot, 'assets')).find(name => /^index-.*\.js$/.test(name))!
  const sha256File = (file: string) => createHash('sha256').update(fs.readFileSync(file)).digest('hex')
  const evidence = {
    captures: manifest,
    provenance: {
      rendererBundle: { path: `apps/desktop/dist/assets/${rendererBundle}`, sha256: sha256File(path.join(distRoot, 'assets', rendererBundle)) },
      rendererIndex: { path: 'apps/desktop/dist/index.html', sha256: sha256File(path.join(distRoot, 'index.html')) },
    },
  }
  fs.writeFileSync(manifestPath, `${JSON.stringify(evidence, null, 2)}\n`)
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
  const overflowingLane = await page.locator('[data-kanban-lane="triage"] > div:last-child').evaluate(element => ({
    overflowY: getComputedStyle(element).overflowY,
    overflows: element.scrollHeight > element.clientHeight,
  }))
  expect(overflowingLane).toEqual({ overflowY: 'auto', overflows: true })
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
  const desktopLaneStyle = await page.locator('[data-kanban-lane="triage"]').evaluate(element => {
    const style = getComputedStyle(element)
    return { borderWidth: style.borderLeftWidth, radius: style.borderRadius }
  })
  expect(desktopLaneStyle.borderWidth).toBe('0px')
  expect(Number.parseFloat(desktopLaneStyle.radius)).toBeGreaterThan(0)
  await page.getByText(LONG_TITLE, { exact: true }).last().click()
  const drawer = page.locator('[data-kanban-layout="desktop"]').last()
  await expect(drawer).toHaveJSProperty('clientWidth', 415) // 26rem outer width minus the 1px shell border
  const actionBox = await page.getByRole('button', { name: 'Task actions' }).boundingBox()
  expect(actionBox?.width).toBe(24)
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBe(0)
})

// eslint-disable-next-line no-empty-pattern
test('767/768 boundary changes toolbar order and snap behavior exactly once', async ({}) => {
  const page = fixture!.page
  seedBoard(fixture!.sandbox.hermesHome)
  await page.reload()

  for (const [width, expected] of [[767, { actionOrder: '0', layout: 'mobile', snap: 'x mandatory' }], [768, { actionOrder: '9999', layout: 'desktop', snap: 'none' }]] as const) {
    await setContentViewport(fixture!, width)
    const state = await page.evaluate(() => {
      const header = document.querySelector<HTMLElement>('[data-kanban-layout]')!
      const children = [...header.children]
      return {
        actionOrder: getComputedStyle(children[1]).order,
        filterOrder: getComputedStyle(children[2]).order,
        layout: header.dataset.kanbanLayout,
        snap: getComputedStyle(document.querySelector<HTMLElement>('[data-kanban-scroller]')!).scrollSnapType,
      }
    })
    expect(state).toMatchObject(expected)
    expect(state.filterOrder).toBe('0')
  }
})

// eslint-disable-next-line no-empty-pattern
test('spawned app leaves hostile canonical sentinel immutable and uses only its sandbox board', async ({}) => {
  expect(fs.realpathSync(canonicalDbLink)).toBe(fs.realpathSync(canonicalDb))
  expect(fs.readFileSync(canonicalDb).equals(canonicalBytes)).toBe(true)
  expect(readBoardIdentity(fixture!.sandbox.hermesHome, canonicalDb)).toEqual(canonicalIdentity)

  const sandboxDb = path.join(fixture!.sandbox.hermesHome, 'kanban.db')
  expect(fs.existsSync(sandboxDb)).toBe(true)
  const sandboxIdentity = readBoardIdentity(fixture!.sandbox.hermesHome)
  expect(sandboxIdentity.tasks.some(([, title]) => title.startsWith('Synthetic '))).toBe(true)
  expect(sandboxIdentity.tasks.some(([, title]) => title === 'canonical spawned-app sentinel')).toBe(false)
})
