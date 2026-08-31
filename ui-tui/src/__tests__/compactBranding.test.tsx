import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { CompactSessionPanel } from '../components/branding.js'
import { DEFAULT_THEME } from '../theme.js'
import type { McpServerStatus, SessionInfo } from '../types.js'

const delay = (ms: number) => new Promise(resolve => setTimeout(resolve, ms))

const mcp = (over: Partial<McpServerStatus> & Pick<McpServerStatus, 'name'>): McpServerStatus => ({
  connected: false,
  tools: 0,
  transport: 'http',
  ...over
})

const info: SessionInfo = {
  cwd: '/workspace/hermes-agent',
  mcp_servers: [
    mcp({ connected: true, name: 'connected', status: 'connected' }),
    mcp({ disabled: true, name: 'disabled', status: 'disabled' })
  ],
  model: 'provider/opus-4.8-fast',
  profile_name: 'coder',
  skills: { development: ['review', 'testing'] },
  tools: { browser: ['browser_click'], file: ['read_file', 'write_file'] },
  version: '3.2.1'
}

async function renderPanel(columns = 88): Promise<string> {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let captured = ''

  Object.assign(stdout, { columns, isTTY: false, rows: 20 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    captured += chunk.toString()
  })

  const instance = renderSync(
    React.createElement(CompactSessionPanel, {
      info,
      maxWidth: columns,
      sid: 'd2a6ecf8',
      t: DEFAULT_THEME
    }),
    {
      patchConsole: false,
      stderr: stderr as NodeJS.WriteStream,
      stdin: stdin as NodeJS.ReadStream,
      stdout: stdout as NodeJS.WriteStream
    }
  )

  try {
    await delay(20)

    // eslint-disable-next-line no-control-regex
    return captured.replace(/\u001b\[[0-9;]*m/g, '')
  } finally {
    instance.unmount()
    instance.cleanup()
  }
}

describe('compact session branding', () => {
  it('keeps session identity and aggregate capability counts in a low-chrome header', async () => {
    const frame = await renderPanel()

    expect(frame).toContain('Hermes Agent')
    expect(frame).toContain('opus-4.8-fast')
    expect(frame).toContain('/workspace/hermes-agent')
    expect(frame).toContain('session d2a6ecf8')
    expect(frame).toContain('3 tools')
    expect(frame).toContain('2 skills')
    expect(frame).toContain('1 MCP')
    expect(frame).not.toContain('browser_click')
    expect(frame).not.toContain('disabled')
  })

  it('progressively hides secondary metadata on narrow terminals', async () => {
    const frame = await renderPanel(36)

    expect(frame).toContain('Hermes Agent')
    expect(frame).toContain('3 tools')
    expect(frame).not.toContain('/workspace/hermes-agent')
    expect(frame).not.toContain('profile coder')
  })
})
