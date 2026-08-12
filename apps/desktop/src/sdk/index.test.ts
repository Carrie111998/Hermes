import { beforeEach, describe, expect, it, vi } from 'vitest'

const composer = vi.hoisted(() => ({
  insert: vi.fn()
}))

vi.mock('@/app/chat/composer/focus', () => ({
  requestComposerInsert: composer.insert
}))

import { host } from './index'

describe('plugin SDK composer action', () => {
  beforeEach(() => {
    composer.insert.mockClear()
  })

  it('inserts text into the active composer through the native composer bus', async () => {
    await host.composer.insert('/multi-agent-project-orchestrator')

    expect(composer.insert).toHaveBeenCalledOnce()
    expect(composer.insert).toHaveBeenCalledWith('/multi-agent-project-orchestrator', {
      mode: 'block',
      target: 'active'
    })
  })

  it('forwards an explicit mode and target', async () => {
    await host.composer.insert('more detail', { mode: 'inline', target: 'main' })

    expect(composer.insert).toHaveBeenCalledWith('more detail', {
      mode: 'inline',
      target: 'main'
    })
  })
})
