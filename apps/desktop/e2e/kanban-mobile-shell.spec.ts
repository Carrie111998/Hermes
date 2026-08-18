import { spawnSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import * as path from 'node:path'

import { type MockBackendFixture, setupMockBackend } from './fixtures'
import { expect, test } from './test'

const REPO_ROOT = path.resolve(import.meta.dirname, '..', '..', '..')
const COLUMNS = ['triage', 'todo', 'scheduled', 'ready', 'running', 'blocked', 'review', 'done']

function seedBoard(hermesHome: string) {
  const source = `
from hermes_cli import kanban_db
kanban_db.init_db()
conn = kanban_db.connect()
columns = ${JSON.stringify(COLUMNS)}
for index, status in enumerate(columns):
    task_id = kanban_db.create_task(
        conn,
        title=f"Synthetic {status} card with deterministic long mobile copy {index}",
        body="Synthetic fixture only. No canonical board or user data.",
        assignee=("fixture-alpha" if index % 2 == 0 else "fixture-beta"),
        created_by="desktop-e2e",
        priority=80 - index,
        initial_status="running",
    )
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
conn.commit()
conn.close()
`

  const result = spawnSync(process.env.PYTHON ?? 'python', ['-c', source], {
    cwd: REPO_ROOT,
    encoding: 'utf8',
    env: { ...process.env, HERMES_HOME: hermesHome, PYTHONPATH: REPO_ROOT }
  })

  if (result.status !== 0) {throw new Error(`Kanban fixture failed: ${result.stderr}`)}
}

let fixture: MockBackendFixture | null = null

test.describe.configure({ timeout: 180_000 })
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
test('actual desktop shell keeps all mobile lanes selectable and contained', async ({}, testInfo) => {
  const page = fixture!.page

  for (const [width, expectedBoardWidth] of [[320, 290], [360, 330], [390, 360], [430, 400]] as const) {
    await fixture!.app.evaluate(({ BrowserWindow }, size) => {
      const window = BrowserWindow.getAllWindows()[0]
      window?.setMinimumSize(0, 0)
      window?.setContentSize(size, 760)
    }, expectedBoardWidth)
    await expect(page.locator('[data-kanban-board-root]')).toHaveJSProperty('clientWidth', expectedBoardWidth)

    for (const [index, status] of COLUMNS.entries()) {
      const selector = page.locator(`[data-kanban-lane-selector="${status}"]`)
      await selector.click()
      await page.waitForTimeout(300)

      const geometry = await page.locator(`[data-kanban-lane="${status}"]`).evaluate(lane => {
        const scroller = lane.parentElement!
        const laneRect = lane.getBoundingClientRect()
        const scrollRect = scroller.getBoundingClientRect()

        const control = document
          .querySelector<HTMLElement>(`[data-kanban-lane-selector="${lane.getAttribute('data-kanban-lane')}"]`)
          ?.getBoundingClientRect()

        return {
          bodyOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
          controlHeight: control?.height,
          controlWidth: control?.width,
          laneLeft: laneRect.left,
          laneRight: laneRect.right,
          scrollLeft: scrollRect.left,
          scrollRight: scrollRect.right
        }
      })

      expect(geometry.bodyOverflow).toBe(0)
      expect(geometry.controlWidth).toBe(44)
      expect(geometry.controlHeight).toBe(44)
      expect(geometry.laneLeft).toBeGreaterThanOrEqual(geometry.scrollLeft)
      expect(geometry.laneRight).toBeLessThanOrEqual(geometry.scrollRight)

      if (index < COLUMNS.length - 1) {
        await selector.press('ArrowRight')
        await expect(page.locator(`[data-kanban-lane-selector="${COLUMNS[index + 1]}"]`)).toBeFocused()
      }
    }

    const bytes = await page.screenshot({ fullPage: true })
    const hash = createHash('sha256').update(bytes).digest('hex')
    await testInfo.attach(`kanban-mobile-${width}-${hash}.png`, { body: bytes, contentType: 'image/png' })
  }
})
