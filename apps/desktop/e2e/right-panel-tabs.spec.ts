import { expect, test } from '@playwright/test'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'

let fixture: MockBackendFixture | null = null

test.beforeAll(async () => {
  fixture = await setupMockBackend({
    extraConfig: `terminal:
  cwd: ${process.cwd()}`
  })
  await waitForAppReady(fixture, 120_000)
})

test.afterAll(async () => {
  await fixture?.cleanup()
  fixture = null
})

test('Files stays outermost while tool functions share the work area to its left', async () => {
  const page = fixture!.page
  const composer = page.locator('[contenteditable="true"]').first()
  const picker = page.getByLabel('Open right panel', { exact: true })

  // A fresh project-less draft intentionally has no Files tree. Creating the
  // session adopts terminal.cwd as its real workspace, exactly like normal use.
  await composer.click()
  await composer.fill('open the project')
  await page.keyboard.press('Enter')
  await page.waitForFunction(
    prompt => (document.querySelector('[data-slot="aui_thread-viewport"]')?.textContent ?? '').includes(prompt),
    'open the project',
    { timeout: 15_000 }
  )

  await picker.click()
  const filesToggle = page.getByRole('menuitemcheckbox', { name: 'File system' })
  await expect(filesToggle).toHaveAttribute('aria-checked', 'false')
  await filesToggle.click()
  await expect(filesToggle).toHaveAttribute('aria-checked', 'true')
  await page.keyboard.press('Escape')

  const filesGroup = page.locator('[data-project-tree]').locator('xpath=ancestor::*[@data-tree-group][1]')

  await expect(filesGroup).toHaveAttribute('data-tree-group', 'grp-files')

  // Picking a file keeps the tree available and opens Preview in the adjacent
  // functional work area instead of replacing Files with another tab.
  await page.locator('[data-project-tree]').getByText('package.json', { exact: true }).click()
  const previewTab = page.locator('[data-tree-tab="preview"]')
  await expect(previewTab).toBeVisible()
  await expect(previewTab.locator('xpath=ancestor::*[@data-tree-group][1]')).toHaveAttribute(
    'data-tree-group',
    'grp-right-tools'
  )
  await expect(page.locator('[data-project-tree]')).toBeVisible()

  const composerRoot = page.locator('[data-slot="composer-root"]:visible').first()
  await expect(composerRoot).toBeVisible()
  expect((await composerRoot.boundingBox())?.width ?? 0).toBeGreaterThanOrEqual(360)

  // The dedicated Explorer shortcut toggles only the structural Files rail;
  // Preview remains open in the functional work area.
  await page.keyboard.press('ControlOrMeta+Shift+E')
  await expect(page.locator('[data-project-tree]')).toBeHidden()
  await expect(previewTab).toBeVisible()

  await page.keyboard.press('ControlOrMeta+Shift+E')
  await expect(page.locator('[data-project-tree]')).toBeVisible()
})
