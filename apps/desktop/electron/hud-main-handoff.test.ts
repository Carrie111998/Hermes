import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const here = path.dirname(fileURLToPath(import.meta.url))
const mainSource = fs.readFileSync(path.join(here, 'main.ts'), 'utf8').replace(/\r\n/g, '\n')

function functionBody(name: string): string {
  const start = mainSource.indexOf(`function ${name}(`)
  expect(start).toBeGreaterThan(-1)

  const rest = mainSource.slice(start)
  const next = rest.slice(1).search(/\nfunction /)

  return next === -1 ? rest : rest.slice(0, next + 1)
}

describe('main HUD close handoff', () => {
  it('broadcasts the latched New Chat generation with the null session', () => {
    expect(mainSource).toMatch(/let hudNewChatGeneration[^=]*= null/)
    expect(functionBody('broadcastHudState')).toContain('newChatGeneration: hudNewChatGeneration')
  })

  it('latches both fields from the authoritative HUD renderer report', () => {
    const registrationStart = mainSource.indexOf('const hudIpc = registerHudIpc({')
    expect(registrationStart).toBeGreaterThan(-1)
    const registration = mainSource.slice(registrationStart, registrationStart + 700)

    expect(registration).toContain('setHudSessionState: state => {')
    expect(registration).toContain('hudSessionId = state.sessionId')
    expect(registration).toContain('hudNewChatGeneration = state.newChatGeneration')
  })

  it('latches the opening New Chat generation and clears it for stored sessions', () => {
    const open = functionBody('openHudWindow')

    expect(open).toContain('latchHudSessionState(sessionId, newChatGeneration)')
    expect(open).toContain('latchHudSessionState(sessionId, null)')
    expect(functionBody('latchHudSessionState')).toContain(
      'hudNewChatGeneration = hudSessionId === null ? newChatGeneration || null : null'
    )
  })
})
