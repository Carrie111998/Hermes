import fs from 'node:fs'
import path from 'node:path'

describe('Sigil Electron startup', () => {
  it('owns a standalone title and persistence namespace', () => {
    const source = fs.readFileSync(path.resolve('electron/main.ts'), 'utf8')
    expect(source).toContain("SIGIL_APP_NAME = 'Sigil'")
    expect(source).toContain("SIGIL_BUNDLE_ID = 'com.firecattechnology.sigil'")
    expect(source).toContain("SIGIL_USER_DATA_DIRECTORY = 'Sigil'")
    expect(source).toContain("title: SIGIL_APP_NAME")
    expect(source).not.toContain('HERMES_DESKTOP')
  })
})
