import { PassThrough } from 'stream'

import { Box, renderSync, Text } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import {
  composerFooterColumns,
  composerOuterMargin,
  ComposerSurface,
  composerSurfaceColumns,
  composerSurfaceWidth
} from '../components/composerSurface.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

const renderSurface = (blocked: boolean, footer?: React.ReactNode) => {
  const stdout = new PassThrough()
  const stdin = new PassThrough()
  const stderr = new PassThrough()
  let output = ''

  Object.assign(stdout, { columns: 48, isTTY: false, rows: 24 })
  Object.assign(stdin, { isTTY: false })
  Object.assign(stderr, { isTTY: false })
  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(
    <ComposerSurface blocked={blocked} cols={48} footer={footer} shell={false} t={DEFAULT_THEME}>
      <Box>
        <Text>draft text</Text>
      </Box>
    </ComposerSurface>,
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

describe('ComposerSurface', () => {
  it('renders an inset GUI-like surface with an integrated accent rail', () => {
    const output = renderSurface(false)

    expect(output).not.toContain('╭')
    expect(output).not.toContain('╰')
    expect(output).not.toContain('│')
    expect(output).toMatch(/^ {4}draft text/m)
    expect(output).toContain('Ask Hermes')
    expect(output).toContain('draft text')
  })

  it('keeps blocked drafts visible and labels their preserved state', () => {
    const output = renderSurface(true)

    expect(output).toContain('Ask Hermes')
    expect(output).toContain('draft preserved')
    expect(output).toContain('draft text')
  })

  it('uses an embedded status rail instead of stacking a second composer label', () => {
    const output = renderSurface(false, <Text>ready · gpt 5.6 · Enter: send</Text>)

    expect(output).toContain('ready · gpt 5.6 · Enter: send')
    expect(output).not.toContain('Ask Hermes')
  })

  it('floats at wide widths and collapses margins before starving narrow input', () => {
    expect(composerOuterMargin(120)).toBe(2)
    expect(composerSurfaceWidth(120)).toBe(114)
    expect(composerSurfaceColumns(120, 2, false)).toBe(105)

    expect(composerOuterMargin(48)).toBe(1)
    expect(composerSurfaceWidth(48)).toBe(44)
    expect(composerSurfaceColumns(48, 2, false)).toBe(35)

    expect(composerOuterMargin(8)).toBe(0)
    expect(composerSurfaceWidth(8)).toBe(6)
    expect(composerSurfaceColumns(8, 2, false)).toBe(1)
  })

  it('reserves narrow footer cells for the blocked-draft state', () => {
    expect(composerFooterColumns(48, false)).toBe(39)
    expect(composerFooterColumns(48, true)).toBe(21)
  })
})
