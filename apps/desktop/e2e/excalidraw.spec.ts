import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import type { Locator, Page } from '@playwright/test'

import { expect, test } from './test'

import { type MockBackendFixture, setupMockBackend, waitForAppReady } from './fixtures'

interface RendererErrors {
  console: string[]
  page: string[]
}

interface ExcalidrawHarness {
  cleanup: () => Promise<void>
  drawingPath: string
  drawingRoot: string
  fixture: MockBackendFixture
  rendererErrors: RendererErrors
}

interface StoredElement {
  id?: string
  isDeleted?: boolean
  [key: string]: unknown
}
interface PositionedStoredElement extends StoredElement {
  id: string
  x: number
  y: number
}

interface PositionedRectangleStoredElement extends PositionedStoredElement {
  type: 'rectangle'
}

const rectangleArea = {
  end: 0.55,
  start: 0.35,
} as const

const rectangleMoveDelta = {
  x: 80,
  y: 60,
} as const

interface StoredDocument {
  elements?: StoredElement[]
}

interface E2EWindow extends Window {
  __HERMES_E2E_GATEWAY_SOCKETS__?: WebSocket[]
}

function drawingDocument(elements: unknown[] = []): string {
  return JSON.stringify({
    appState: { viewBackgroundColor: '#ffffff' },
    elements,
    files: {},
    source: 'hermes-desktop-e2e',
    type: 'excalidraw',
    version: 2,
  })
}

function readLiveElements(documentPath: string): StoredElement[] {
  const document = JSON.parse(fs.readFileSync(documentPath, 'utf8')) as StoredDocument

  return (document.elements ?? []).filter(element => !element.isDeleted)
}

function isPositionedStoredElement(element: StoredElement): element is PositionedStoredElement {
  return typeof element.id === 'string' && typeof element.x === 'number' && typeof element.y === 'number'
}

function isPositionedRectangleStoredElement(element: StoredElement): element is PositionedRectangleStoredElement {
  return element.type === 'rectangle' && isPositionedStoredElement(element)
}

async function visibleCanvas(page: Page): Promise<Locator> {
  const canvas = page.locator('canvas.excalidraw__canvas.interactive').first()
  await expect(canvas).toBeVisible({ timeout: 30_000 })
  await expect
    .poll(async () => {
      const bounds = await canvas.boundingBox()

      return bounds ? Math.min(bounds.width, bounds.height) : 0
    }, { message: 'the real Excalidraw canvas should have non-zero bounds' })
    .toBeGreaterThan(0)

  return canvas
}

async function drawRectangle(page: Page, canvas: Locator): Promise<void> {
  const bounds = await canvas.boundingBox()
  expect(bounds, 'the real editor must remain measurable before drawing').not.toBeNull()
  await canvas.click({ position: { x: bounds!.width * 0.2, y: bounds!.height * 0.2 } })
  await page.keyboard.press('2')
  await page.mouse.move(
    bounds!.x + bounds!.width * rectangleArea.start,
    bounds!.y + bounds!.height * rectangleArea.start,
  )
  await page.mouse.down()
  await page.mouse.move(
    bounds!.x + bounds!.width * rectangleArea.end,
    bounds!.y + bounds!.height * rectangleArea.end,
    { steps: 8 },
  )
  await page.mouse.up()
}

async function moveRectangle(page: Page, canvas: Locator): Promise<void> {
  const bounds = await canvas.boundingBox()
  expect(bounds, 'the real editor must remain measurable before moving').not.toBeNull()
  await page.keyboard.press('v')
  await page.mouse.move(
    bounds!.x + bounds!.width * ((rectangleArea.start + rectangleArea.end) / 2),
    bounds!.y + bounds!.height * ((rectangleArea.start + rectangleArea.end) / 2),
  )
  await page.mouse.down()
  await page.mouse.move(
    bounds!.x + bounds!.width * ((rectangleArea.start + rectangleArea.end) / 2) + rectangleMoveDelta.x,
    bounds!.y + bounds!.height * ((rectangleArea.start + rectangleArea.end) / 2) + rectangleMoveDelta.y,
    { steps: 8 },
  )
  await page.mouse.up()
}

async function bootstrapExcalidraw(initialElements: unknown[] = []): Promise<ExcalidrawHarness> {
  const drawingRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-e2e-excalidraw-'))
  const drawingPath = path.join(drawingRoot, 'real-editor.excalidraw')
  fs.writeFileSync(drawingPath, drawingDocument(initialElements), 'utf8')

  let fixture: MockBackendFixture | undefined
  const rendererErrors: RendererErrors = { console: [], page: [] }
  try {
    fixture = await setupMockBackend()
    fixture.page.on('pageerror', error => rendererErrors.page.push(error.message))
    fixture.page.on('console', message => {
      if (message.type() === 'error') rendererErrors.console.push(message.text())
    })
    await waitForAppReady(fixture, 120_000)
    await fixture.page.addInitScript(
      ({ documentPath }) => {
        const nativeWebSocket = window.WebSocket
        const sockets: WebSocket[] = []
        ;(window as E2EWindow).__HERMES_E2E_GATEWAY_SOCKETS__ = sockets
        window.WebSocket = new Proxy(nativeWebSocket, {
          construct(target, args) {
            const socket = Reflect.construct(target, args) as WebSocket
            sockets.push(socket)

            return socket
          },
        }) as typeof WebSocket

        localStorage.setItem(
          'hermes.desktop.excalidraw.documents',
          JSON.stringify([
            {
              fingerprint: 'e2e-seed',
              identity: { path: documentPath, profile: 'default', runtime: 'local' },
              status: 'connected',
            },
          ]),
        )
        const paneId = `excalidraw:${encodeURIComponent(`default\u0000local\u0000${documentPath}`)}`
        localStorage.setItem(
          'hermes.desktop.layoutTree.v2',
          JSON.stringify({
            children: [
              { active: 'workspace', id: 'e2e-main', panes: ['workspace'], type: 'group' },
              { active: paneId, id: 'e2e-drawing', panes: [paneId], type: 'group' },
            ],
            id: 'e2e-excalidraw-root',
            orientation: 'row',
            type: 'split',
            weights: [3, 1],
          }),
        )
        const paneStates = JSON.parse(localStorage.getItem('hermes.desktop.paneStates.v1') ?? '{}') as Record<
          string,
          { open?: boolean; widthOverride?: number }
        >
        localStorage.setItem(
          'hermes.desktop.paneStates.v1',
          JSON.stringify({ ...paneStates, 'file-browser': { ...paneStates['file-browser'], open: true } }),
        )
      },
      { documentPath: drawingPath },
    )
    await fixture.page.reload()
    await waitForAppReady(fixture, 120_000)
    await visibleCanvas(fixture.page)

    return {
      drawingPath,
      drawingRoot,
      fixture,
      rendererErrors,
      cleanup: async () => {
        try {
          await fixture!.cleanup()
        } finally {
          fs.rmSync(drawingRoot, { force: true, recursive: true })
        }
      },
    }
  } catch (error) {
    try {
      await fixture?.cleanup()
    } catch {
      // Preserve the bootstrap error that caused cleanup.
    }
    try {
      fs.rmSync(drawingRoot, { force: true, recursive: true })
    } catch {
      // Preserve the bootstrap error that caused cleanup.
    }
    throw new Error(
      `Failed to bootstrap Excalidraw E2E harness. Captured renderer errors — page: ${JSON.stringify(rendererErrors.page)}; console: ${JSON.stringify(rendererErrors.console)}`,
      { cause: error },
    )
  }
}

let harness: ExcalidrawHarness | undefined

function activeHarness(): ExcalidrawHarness {
  expect(harness, 'the Excalidraw E2E harness should be initialized').toBeDefined()

  return harness!
}

test.beforeEach(async () => {
  harness = await bootstrapExcalidraw()
})

test.afterEach(async () => {
  const active = harness
  harness = undefined

  if (!active) return

  try {
    expect(active.rendererErrors, `renderer errors: ${JSON.stringify(active.rendererErrors)}`).toEqual({
      console: [],
      page: [],
    })
  } finally {
    await active.cleanup()
  }
})

/*
 * A bounded external-refresh E2E dispatched a synthetic `excalidraw.changed`
 * event: the old canvas disconnected and a visible, non-zero replacement canvas mounted.
 * Its next gesture hit `This drawing changed on disk` and did not persist a second
 * element, so the unstable synthetic scenario is not retained. External remount
 * behavior remains unit-covered in src/plugins/excalidraw/ExcalidrawPane.test.tsx.
 */
test('draws, moves, and persists a real editor rectangle without a render feedback loop', async () => {
  const active = activeHarness()
  const canvas = await visibleCanvas(active.fixture.page)

  await drawRectangle(active.fixture.page, canvas)

  await expect
    .poll(
      () => readLiveElements(active.drawingPath).some(isPositionedRectangleStoredElement),
      {
        message: `the real editor should persist a positioned live rectangle to ${active.drawingPath}`,
        timeout: 15_000,
      },
    )
    .toBe(true)
  const initialElement = readLiveElements(active.drawingPath).find(isPositionedRectangleStoredElement)
  expect(initialElement, 'the persisted rectangle should have a stable ID and numeric coordinates').toBeDefined()
  if (!initialElement) throw new Error('the persisted rectangle is missing stable coordinates')

  const { id: initialElementId, x: initialX, y: initialY } = initialElement

  await moveRectangle(active.fixture.page, canvas)

  await expect
    .poll(
      () => {
        const movedElement = readLiveElements(active.drawingPath).find(element => element.id === initialElementId)

        if (!movedElement || !isPositionedRectangleStoredElement(movedElement)) return false
        return movedElement.x !== initialX || movedElement.y !== initialY
      },
      {
        message: `the real editor should persist moved coordinates for element ${initialElementId}`,
        timeout: 15_000,
      },
    )
    .toBe(true)
  await expect(canvas).toBeVisible()
  const finalBounds = await canvas.boundingBox()
  expect(finalBounds?.width ?? 0).toBeGreaterThan(0)
  expect(finalBounds?.height ?? 0).toBeGreaterThan(0)
})
