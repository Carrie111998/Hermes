import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { StatusRule } from '../components/appChrome.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

function renderStatus(overrides: Record<string, unknown> = {}) {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 160, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    React.createElement(StatusRule, {
      bgCount: 0,
      busy: false,
      busyInputMode: 'queue',
      cols: 160,
      cwdLabel: '~/src/hermes-agent',
      liveSessionCount: 0,
      model: 'openai/gpt-5.6-sol',
      profile: 'coder',
      queueCount: 0,
      shell: false,
      status: 'ready',
      statusColor: DEFAULT_THEME.color.ok,
      t: DEFAULT_THEME,
      usage: { calls: 0, input: 0, output: 0, total: 0 },
      ...overrides
    }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  instance.unmount()
  instance.cleanup()

  return stripAnsi(output)
}

describe('StatusRule composer state', () => {
  it('shows profile, busy submit behavior, and collapsed queue count', () => {
    const out = renderStatus({ busy: true, queueCount: 2 })

    expect(out).toContain('coder')
    expect(out).toContain('Enter: queue')
    expect(out).toContain('Queued 2')
  })

  it('labels shell mode without relying on prompt color', () => {
    expect(renderStatus({ shell: true })).toContain('Shell')
  })

  it('does not spend rail width on default profile names', () => {
    expect(renderStatus({ profile: 'default' })).not.toContain('default')
  })

  it('embeds status content without the detached rule or cwd tail', () => {
    const out = renderStatus({ embedded: true })

    expect(out).toContain('ready')
    expect(out).toContain('gpt 5.6 sol')
    expect(out).toContain('Enter: send')
    expect(out).not.toContain('─')
    expect(out).not.toContain('~/src/hermes-agent')
  })

  it('keeps the active Enter behavior ahead of profile and session metadata on narrow composers', () => {
    const out = renderStatus({ cols: 39, embedded: true, liveSessionCount: 1, profile: 'blank' })

    expect(out).toContain('Enter: send')
    expect(out).not.toContain('1 session')
    expect(out).not.toContain('blank')
  })
})
