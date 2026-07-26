import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import React from 'react'
import { describe, expect, it } from 'vitest'

import { MessageLine } from '../components/messageLine.js'
import { fmtDuration, toTranscriptMessages } from '../domain/messages.js'
import { upsert } from '../lib/messages.js'
import { stripAnsi } from '../lib/text.js'
import { DEFAULT_THEME } from '../theme.js'

describe('toTranscriptMessages', () => {
  it('preserves assistant tool-call rows so resume does not drop prior turns', () => {
    const rows = [
      { role: 'user', text: 'first prompt' },
      { role: 'tool', context: 'repo', name: 'search_files', text: 'ignored raw result' },
      { role: 'assistant', text: 'first answer' },
      { role: 'user', text: 'second prompt' }
    ]

    expect(toTranscriptMessages(rows).map(msg => [msg.role, msg.text])).toEqual([
      ['user', 'first prompt'],
      ['assistant', 'first answer'],
      ['user', 'second prompt']
    ])
    expect(toTranscriptMessages(rows)[1]?.tools?.[0]).toContain('Search Files')
  })

  it('skips hidden display_kind rows entirely', () => {
    const rows = [
      { role: 'user', text: 'visible prompt' },
      { role: 'user', text: '[CONTEXT COMPACTION — REFERENCE ONLY]', display_kind: 'hidden' },
      { role: 'assistant', text: 'visible reply' }
    ]

    const result = toTranscriptMessages(rows)
    expect(result.map(msg => msg.text)).toEqual(['visible prompt', 'visible reply'])
    expect(result.every(m => !m.text?.includes('COMPACTION'))).toBe(true)
  })

  it('projects model_switch as an event with replaced text', () => {
    const rows = [
      { role: 'user', text: 'hello' },
      { role: 'user', text: '[System: model changed to gpt-5]', display_kind: 'model_switch' },
      { role: 'assistant', text: 'hi' }
    ]

    const result = toTranscriptMessages(rows)
    expect(result.map(msg => [msg.kind, msg.role, msg.text])).toEqual([
      [undefined, 'user', 'hello'],
      ['event', 'system', 'model changed'],
      [undefined, 'assistant', 'hi']
    ])
  })

  it('projects async_delegation_complete with task_count metadata', () => {
    const rows = [
      { role: 'user', text: 'do work' },
      { role: 'assistant', text: 'done' },
      {
        role: 'user',
        text: '[IMPORTANT: delegation done]',
        display_kind: 'async_delegation_complete',
        display_metadata: { task_count: 3 }
      },
      { role: 'assistant', text: 'merged' }
    ]

    const result = toTranscriptMessages(rows)
    expect(result.map(msg => [msg.kind, msg.text])).toEqual([
      [undefined, 'do work'],
      [undefined, 'done'],
      ['event', '3 background agents finished'],
      [undefined, 'merged']
    ])
  })

  it('projects async_delegation_complete without metadata as generic text', () => {
    const rows = [{ role: 'user', text: 'event', display_kind: 'async_delegation_complete' }]

    const result = toTranscriptMessages(rows)
    expect(result[0]?.kind).toBe('event')
    expect(result[0]?.text).toBe('background agent work finished')
  })
})

describe('MessageLine', () => {
  it('preserves a separator after compound user prompt glyphs in transcript rows', () => {
    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    let output = ''

    Object.assign(stdout, { columns: 80, isTTY: false, rows: 24 })
    Object.assign(stdin, { isTTY: false })
    Object.assign(stderr, { isTTY: false })
    stdout.on('data', chunk => {
      output += chunk.toString()
    })

    const t = {
      ...DEFAULT_THEME,
      brand: { ...DEFAULT_THEME.brand, prompt: 'Ψ >' }
    }

    const instance = renderSync(
      React.createElement(MessageLine, {
        cols: 80,
        msg: { role: 'user', text: 'Okay' },
        t
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

    const renderedLine = stripAnsi(output)
      .split('\n')
      .find(line => line.includes('Okay'))

    expect(renderedLine).toContain('Ψ > Okay')
  })
})

describe('upsert', () => {
  it('appends when last role differs', () => {
    expect(upsert([{ role: 'user', text: 'hi' }], 'assistant', 'hello')).toHaveLength(2)
  })

  it('replaces when last role matches', () => {
    expect(upsert([{ role: 'assistant', text: 'partial' }], 'assistant', 'full')[0]!.text).toBe('full')
  })

  it('appends to empty', () => {
    expect(upsert([], 'user', 'first')).toEqual([{ role: 'user', text: 'first' }])
  })

  it('does not mutate', () => {
    const prev = [{ role: 'user' as const, text: 'hi' }]
    upsert(prev, 'assistant', 'yo')
    expect(prev).toHaveLength(1)
  })
})

describe('fmtDuration', () => {
  it('formats under a minute as plain seconds', () => {
    expect(fmtDuration(0)).toBe('0s')
    expect(fmtDuration(42_000)).toBe('42s')
    expect(fmtDuration(59_400)).toBe('59s')
  })

  it('formats whole minutes with trailing seconds', () => {
    expect(fmtDuration(60_000)).toBe('1m 0s')
    expect(fmtDuration(180_000)).toBe('3m 0s')
  })

  it('mixes minutes and seconds', () => {
    expect(fmtDuration(134_000)).toBe('2m 14s')
    expect(fmtDuration(605_000)).toBe('10m 5s')
  })

  it('formats whole hours with trailing minutes', () => {
    expect(fmtDuration(3_600_000)).toBe('1h 0m')
    expect(fmtDuration(7_200_000)).toBe('2h 0m')
  })

  it('mixes hours and minutes', () => {
    expect(fmtDuration(5_400_000)).toBe('1h 30m')
    expect(fmtDuration(3_960_000)).toBe('1h 6m')
  })

  it('formats whole days without trailing hours', () => {
    expect(fmtDuration(86_400_000)).toBe('1d')
    expect(fmtDuration(172_800_000)).toBe('2d')
  })

  it('mixes days and hours', () => {
    expect(fmtDuration(129_600_000)).toBe('1d 12h') // 1.5 days
    expect(fmtDuration(97_200_000)).toBe('1d 3h') // 1d 3h
  })
})
