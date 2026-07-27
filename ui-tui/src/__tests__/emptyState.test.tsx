import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { CompactWelcome } from '../components/emptyState.js'
import { stripAnsi } from '../lib/text.js'
import { estimatedMsgHeight } from '../lib/virtualHeights.js'
import { DEFAULT_THEME } from '../theme.js'
import type { SessionInfo } from '../types.js'

const info: SessionInfo = {
  cwd: '/Users/main/Workspace/developer/applications/hermes-agent',
  model: 'openai/gpt-5.6-sol',
  profile_name: 'coder',
  skills: { software_development: ['test-driven-development'] },
  tools: { core_tools: ['terminal', 'read_file'] }
}

function renderWelcome(cols: number, value: null | SessionInfo = info) {
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

  const instance = renderSync(React.createElement(CompactWelcome, { cols, info: value, t: DEFAULT_THEME }), {
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  const rendered = stripAnsi(output)

  instance.unmount()
  instance.cleanup()

  return rendered
}

const renderedWelcomeHeight = (value: null | SessionInfo) =>
  renderWelcome(80, value).replace(/\n$/, '').split('\n').length

describe('CompactWelcome', () => {
  it('keeps the first viewport focused on workspace and next actions', () => {
    const out = renderWelcome(100)

    expect(out).toContain('Hermes')
    expect(out).toContain('hermes-agent')
    expect(out).toContain('coder')
    expect(out).toContain('gpt-5.6-sol')
    expect(out).toContain('Ctrl+P actions')
    expect(out).toContain('@ context')
    expect(out).not.toContain('test-driven-development')
    expect(out).not.toContain('terminal, read_file')
  })

  it('uses a bounded basename-oriented layout on narrow terminals', () => {
    const out = renderWelcome(40)

    expect(out).toContain('Hermes')
    expect(out).toContain('hermes-agent')
    expect(out).not.toContain('/Users/main/Workspace')
  })

  it('shows a calm starting state before session info arrives', () => {
    expect(renderWelcome(80, null)).toContain('Starting session')
  })

  it.each([
    info,
    { ...info, version: '1.0.0' },
    { ...info, install_warning: 'Install needs attention', update_behind: 2 }
  ])('keeps intro virtualization equal to the compact welcome rows', value => {
    expect(
      estimatedMsgHeight({ kind: 'intro', role: 'system', text: '', info: value }, 80, {
        compact: false,
        details: false
      })
    ).toBe(renderedWelcomeHeight(value))
  })
})
