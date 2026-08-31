import { describe, expect, it } from 'vitest'

import { applicationWindowMenu } from './application-menu'

describe('applicationWindowMenu', () => {
  it('delegates the macOS Window menu to Electron native role', () => {
    expect(applicationWindowMenu('darwin')).toEqual({ role: 'windowMenu' })
  })

  it('keeps the compact Window menu on Windows and Linux', () => {
    const expected = {
      label: 'Window',
      submenu: [{ role: 'minimize' }, { role: 'close' }]
    }

    expect(applicationWindowMenu('win32')).toEqual(expected)
    expect(applicationWindowMenu('linux')).toEqual(expected)
  })
})
