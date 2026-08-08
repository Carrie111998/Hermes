import { describe, expect, it } from 'vitest'

import { stripTerminalControlFragments } from '../lib/terminalInputSanitize.js'

// The reported blob: a burst of SGR motion reports (button 35) + wheel
// (64/65) with the ESC[< prefixes consumed, plus DECRPM/DA1 response tails.
const LEAKED_MOUSE_RUN = '35;8;1M35;9;1M35;10;1M35;10;2M35;11;2M35;11;3M35;12;3M35;12;4M'
const LEAKED_HEAD = '1000;1$y62;22c'

describe('stripTerminalControlFragments', () => {
  it('strips ESC-form SGR mouse reports', () => {
    expect(stripTerminalControlFragments('a\x1b[<35;8;1Mb')).toBe('ab')
    expect(stripTerminalControlFragments('x\x1b[<64;40;22My')).toBe('xy')
    expect(stripTerminalControlFragments('x\x1b[<65;40;22my')).toBe('xy')
  })

  it('strips visible caret-form mouse reports', () => {
    expect(stripTerminalControlFragments('a^[[<35;8;1Mb')).toBe('ab')
  })

  it('strips bare <b;x;yM fragments', () => {
    expect(stripTerminalControlFragments('a<35;8;1Mb')).toBe('ab')
  })

  it('strips a leaked bare mouse-report run (the reported blob)', () => {
    const input = `${LEAKED_HEAD}${LEAKED_MOUSE_RUN}`
    // DECRPM head stripped; DA1 tail (62;22c) has no bare form by design;
    // the mouse-report run is removed.
    expect(stripTerminalControlFragments(input)).toBe('62;22c')
    expect(stripTerminalControlFragments(LEAKED_MOUSE_RUN)).toBe('')
  })

  it('strips ESC-form and visible CPR/DSR responses', () => {
    expect(stripTerminalControlFragments('a\x1b[24;80Rb')).toBe('ab')
    expect(stripTerminalControlFragments('a^[[24;80Rb')).toBe('ab')
  })

  it('strips DECRPM responses (ESC, visible, bare)', () => {
    expect(stripTerminalControlFragments('a\x1b[?1000;1$yb')).toBe('ab')
    expect(stripTerminalControlFragments('a^[[?1000;1$yb')).toBe('ab')
    expect(stripTerminalControlFragments(`${LEAKED_HEAD}rest`)).toBe('62;22crest')
  })

  it('keeps a single isolated mouse-shaped token in prose', () => {
    // One `b;x;yM`-shaped token could be typed data (e.g. a version string);
    // only ESC/bare-< forms and runs of >=3 are stripped.
    expect(stripTerminalControlFragments('result: 35;8;1M done')).toBe('result: 35;8;1M done')
  })

  it('keeps normal prose untouched', () => {
    const prose = 'the quick brown fox jumps over the lazy dog; 1000 lines; y62 next'
    expect(stripTerminalControlFragments(prose)).toBe(prose)
  })

  it('handles empty input', () => {
    expect(stripTerminalControlFragments('')).toBe('')
    expect(stripTerminalControlFragments('\n')).toBe('\n')
  })
})
