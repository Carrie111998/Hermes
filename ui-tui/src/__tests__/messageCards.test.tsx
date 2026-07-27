import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { messageCardWidth, MessageLine } from '../components/messageLine.js'
import { stripAnsi } from '../lib/text.js'
import { estimatedMsgHeight } from '../lib/virtualHeights.js'
import { DEFAULT_THEME } from '../theme.js'
import type { Msg } from '../types.js'

const renderMessage = (msg: Msg, cols = 80) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: cols, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    React.createElement(MessageLine, {
      cols,
      msg,
      t: DEFAULT_THEME
    }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  const rendered = stripAnsi(output)

  instance.unmount()
  instance.cleanup()

  return rendered
}

const renderedHeight = (msg: Msg, cols: number) => renderMessage(msg, cols).replace(/\n$/, '').split('\n').length

describe('conversation message cards', () => {
  it('reserves transcript padding, scrollbar, and a terminal guard column', () => {
    expect(messageCardWidth(120)).toBe(92)
    expect(messageCardWidth(48)).toBe(44)
    expect(messageCardWidth(8)).toBe(4)
  })

  it('uses asymmetric alignment to make wide transcripts read like a conversation', () => {
    const user = renderMessage({ role: 'user', text: 'right aligned' })
    const assistant = renderMessage({ role: 'assistant', text: 'left aligned' })
    const userTop = user.split('\n').find(line => line.includes('You')) ?? ''
    const assistantTop = assistant.split('\n').find(line => line.includes('Hermes')) ?? ''

    expect(userTop.indexOf('You')).toBeGreaterThan(assistantTop.indexOf('Hermes'))
  })

  it('renders user prompts as a flat labeled color surface without retaining the composer arrow', () => {
    const output = renderMessage({ role: 'user', text: 'Please review this change.' })

    expect(output).not.toContain('╭')
    expect(output).not.toContain('│')
    expect(output).not.toContain('╰')
    expect(output).not.toContain(DEFAULT_THEME.brand.prompt)
    expect(output).toContain('You')
    expect(output).toContain('Please review this change.')
  })

  it('renders assistant responses on the same flat card vocabulary without an inner gutter rail', () => {
    const output = renderMessage({ role: 'assistant', text: 'I found two issues.' })

    expect(output).not.toContain('╭')
    expect(output).not.toContain('│')
    expect(output).not.toContain('╰')
    expect(output).not.toContain(DEFAULT_THEME.brand.tool)
    expect(output).toContain('Hermes')
    expect(output).toContain('I found two issues.')
  })

  it('keeps system timeline text compact instead of turning every event into a card', () => {
    const output = renderMessage({ role: 'system', text: 'session ready' })

    expect(output).not.toContain('╭')
    expect(output).toContain('session ready')
  })

  it.each([
    { cols: 80, msg: { role: 'user', text: 'short prompt' } as Msg },
    { cols: 80, msg: { role: 'assistant', text: 'short response' } as Msg },
    { cols: 48, msg: { role: 'user', text: 'wrapped prompt '.repeat(8) } as Msg },
    { cols: 48, msg: { role: 'assistant', text: 'wrapped response '.repeat(8) } as Msg }
  ])('matches rendered card rows for $msg.role at $cols columns', ({ cols, msg }) => {
    expect(estimatedMsgHeight(msg, cols, { compact: false, details: false })).toBe(renderedHeight(msg, cols))
  })
})
