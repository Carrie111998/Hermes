import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createPluginContext } from './plugin'

describe('createPluginContext.onDispose', () => {
  it('collects arbitrary cleanups so the host runs them on deactivate', () => {
    const disposers: Array<() => void> = []
    const ctx = createPluginContext('demo', dispose => disposers.push(dispose))

    let cleaned = false
    ctx.onDispose(() => {
      cleaned = true
    })

    // The cleanup is tracked alongside contribution/socket disposers, so the
    // loader's deactivate (which runs every collected disposer) tears it down.
    expect(disposers).toHaveLength(1)
    disposers.forEach(dispose => dispose())
    expect(cleaned).toBe(true)
  })
})

describe('createPluginContext.download', () => {
  const pluginDownload = vi.fn(async () => ({ canceled: false, filePath: '/tmp/a.png' }))
  const pluginRevealDownload = vi.fn(async () => true)

  beforeEach(() => {
    pluginDownload.mockClear()
    pluginRevealDownload.mockClear()
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { pluginDownload, pluginRevealDownload },
      writable: true
    })
  })

  it('stamps the calling plugin id so a plugin can only reach its own namespace', async () => {
    await createPluginContext('kanban').download('/attachments/7', { filename: 'shot.png' })

    // The renderer never assembles a URL — it hands main the id + relative
    // path, and main rebuilds `/api/plugins/<id>/...` from them.
    expect(pluginDownload).toHaveBeenCalledWith(
      expect.objectContaining({ filename: 'shot.png', path: '/attachments/7', pluginId: 'kanban' })
    )
  })

  it('normalizes a bare path and rejects traversal out of the namespace', async () => {
    const ctx = createPluginContext('kanban')

    await ctx.download('attachments/7')
    expect(pluginDownload).toHaveBeenCalledWith(expect.objectContaining({ path: '/attachments/7' }))

    // Same guard `rest` and `socket` use: `..` can't normalize into another
    // plugin's API or a core route.
    await expect(ctx.download('/../other/secrets')).rejects.toThrow(/traversal/)
    expect(pluginDownload).toHaveBeenCalledTimes(1)
  })

  it('surfaces a user-cancelled save as a normal result, not an error', async () => {
    pluginDownload.mockResolvedValueOnce({ canceled: true } as never)

    await expect(createPluginContext('kanban').download('/attachments/7')).resolves.toEqual({ canceled: true })
  })

  it('fails with a clear message on a shell that predates the channel', async () => {
    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: {}, writable: true })

    await expect(createPluginContext('kanban').download('/attachments/7')).rejects.toThrow(/cannot download/i)
  })

  it('reveals a saved file, and resolves false when the shell cannot', async () => {
    await expect(createPluginContext('kanban').revealDownload('/tmp/a.png')).resolves.toBe(true)
    expect(pluginRevealDownload).toHaveBeenCalledWith('/tmp/a.png')

    Object.defineProperty(window, 'hermesDesktop', { configurable: true, value: {}, writable: true })
    await expect(createPluginContext('kanban').revealDownload('/tmp/a.png')).resolves.toBe(false)
  })
})
