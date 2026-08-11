import { describe, expect, it } from 'vitest'

import * as sdk from './index'

describe('runtime plugin SDK boundary', () => {
  it('does not expose privileged host internals to runtime plugins', () => {
    expect(sdk).not.toHaveProperty('$connection')
    expect(sdk).not.toHaveProperty('$layoutTree')
    expect(sdk).not.toHaveProperty('Codecs')
    expect(sdk).not.toHaveProperty('desktopFsCacheKey')
    expect(sdk).not.toHaveProperty('isDesktopFsRemoteMode')
    expect(sdk).not.toHaveProperty('persistentAtom')
    expect(sdk).not.toHaveProperty('readDesktopFileText')
    expect(sdk).not.toHaveProperty('registerPaneCloser')
    expect(sdk).not.toHaveProperty('removeTreePane')
    expect(sdk).not.toHaveProperty('revealTreePane')
    expect(sdk).not.toHaveProperty('selectDesktopPaths')
    expect(sdk).not.toHaveProperty('registry')
    expect(sdk).not.toHaveProperty('writeDesktopFileText')
  })
})
