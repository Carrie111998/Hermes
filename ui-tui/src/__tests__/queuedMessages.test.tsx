import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { QueuedMessages } from '../components/queuedMessages.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

function renderQueue(queueEditIdx: null | number) {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 100, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    React.createElement(QueuedMessages, {
      cols: 100,
      queued: ['first queued prompt', 'second queued prompt'],
      queueEditIdx,
      t: DEFAULT_THEME
    }),
    {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    }
  )

  instance.unmount()
  instance.cleanup()

  return stripAnsi(output)
}

describe('QueuedMessages progressive disclosure', () => {
  it('collapses the queue into the composer rail until queue editing is active', () => {
    expect(renderQueue(null).trim()).toBe('')
  })

  it('shows the focused queue window while editing', () => {
    const out = renderQueue(0)

    expect(out).toContain('queued (2)')
    expect(out).toContain('first queued prompt')
    expect(out).toContain('second queued prompt')
  })
})
