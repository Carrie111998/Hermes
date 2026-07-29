import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const STORAGE_KEY = 'hermes.desktop.layoutTree.v2'

async function loadToolZone(
  minimized?: boolean,
  terminalInitiallyOpen = true,
  logsInitiallyOpen = true,
  active: 'logs' | 'terminal' = 'terminal'
) {
  const model = await import('./model')

  const persisted = model.split(
    'column',
    [
      model.group(['workspace'], { id: 'grp-main' }),
      model.group(['terminal', 'logs'], {
        active,
        id: 'grp-tools',
        minimized
      })
    ],
    [3, 1],
    'spl-root'
  )

  const defaultTree = model.split(
    'column',
    [
      model.group(['workspace'], { id: 'grp-main' }),
      model.group(['terminal', 'logs'], {
        active: 'terminal',
        id: 'grp-tools',
        minimized: false
      })
    ],
    [3, 1],
    'spl-root'
  )

  window.localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted))

  const tree = await import('./store')
  const terminalOpen = atom(terminalInitiallyOpen)
  const logsOpen = atom(logsInitiallyOpen)

  tree.declareDefaultTree(defaultTree)

  for (const [paneId, open] of [
    ['terminal', terminalOpen],
    ['logs', logsOpen]
  ] as const) {
    tree.bindPaneCollapse(
      paneId,
      open,
      () => open.set(false),
      () => open.set(true)
    )
  }

  return { logsOpen, model, terminalOpen, tree }
}

describe('tool-zone collapse persistence', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('keeps a persisted minimized tool zone collapsed during boot binding', async () => {
    const { logsOpen, model, terminalOpen, tree } = await loadToolZone(true)

    expect(model.findGroup(tree.$layoutTree.get()!, 'grp-tools')?.minimized).toBe(true)
    expect(terminalOpen.get()).toBe(false)
    expect(logsOpen.get()).toBe(false)
  })

  it('reopens only the active pane when the persisted tool zone is expanded', async () => {
    const { logsOpen, model, terminalOpen, tree } = await loadToolZone(false, false, false, 'logs')

    expect(model.findGroup(tree.$layoutTree.get()!, 'grp-tools')?.minimized).toBe(false)
    expect(terminalOpen.get()).toBe(false)
    expect(logsOpen.get()).toBe(true)
  })

  it('keeps legacy toggle migration when the tree has no minimized value', async () => {
    const { logsOpen, model, terminalOpen, tree } = await loadToolZone(undefined, false, true)

    expect(model.findGroup(tree.$layoutTree.get()!, 'grp-tools')?.minimized).toBe(false)
    expect(model.findGroup(tree.$layoutTree.get()!, 'grp-tools')?.active).toBe('logs')
  })

  it('keeps post-boot toggle changes synchronized with the restored tree', async () => {
    const { model, terminalOpen, tree } = await loadToolZone(true)

    terminalOpen.set(true)
    expect(model.findGroup(tree.$layoutTree.get()!, 'grp-tools')?.minimized).toBe(false)

    terminalOpen.set(false)
    expect(model.findGroup(tree.$layoutTree.get()!, 'grp-tools')?.minimized).toBe(true)
  })

  it('reconciles toggle stores when layout reset replaces the persisted tree', async () => {
    const { logsOpen, model, terminalOpen, tree } = await loadToolZone(true)

    tree.resetLayoutTree()

    expect(model.findGroup(tree.$layoutTree.get()!, 'grp-tools')?.minimized).toBe(false)
    expect(model.findGroup(tree.$layoutTree.get()!, 'grp-tools')?.active).toBe('terminal')
    expect(terminalOpen.get()).toBe(true)
    expect(logsOpen.get()).toBe(false)
  })
})
