import { afterEach, describe, expect, it } from 'vitest'

import { clearMobileFileStoreForTests } from './mobile-files'
import { makeStubs } from './stubs'

afterEach(() => {
  clearMobileFileStoreForTests()
})

describe('mobile bridge file stubs', () => {
  it('turns a browser-selected File into an opaque bridge handle that can be uploaded', async () => {
    const bridge = makeStubs()
    const file = new File(['mobile attachment'], 'brief.txt', { type: 'text/plain' })

    const path = bridge.getPathForFile(file)

    expect(path).toMatch(/^mobile-file:\/\//)
    expect(path).not.toContain('brief.txt')
    await expect(bridge.readFileDataUrlForAttach(path)).resolves.toBe('data:text/plain;base64,bW9iaWxlIGF0dGFjaG1lbnQ=')
  })

  it('does not offer broad folder selection on mobile', async () => {
    const bridge = makeStubs()

    await expect(bridge.selectPaths({ directories: true })).resolves.toEqual([])
  })

  it('keeps image bytes from an explicit capture/picker action behind the same opaque handle', async () => {
    const bridge = makeStubs()

    const path = await bridge.saveImageBuffer(new Uint8Array([137, 80, 78, 71]), '.png')

    expect(path).toMatch(/^mobile-file:\/\//)
    await expect(bridge.readFileDataUrl(path)).resolves.toBe('data:image/png;base64,iVBORw==')
  })
})
