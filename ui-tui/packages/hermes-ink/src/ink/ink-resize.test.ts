import { EventEmitter } from 'events'

import React from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TerminalSizeContext } from './components/TerminalSizeContext.js'
import Text from './components/Text.js'
import Ink from './ink.js'
import { CURSOR_HOME, ERASE_SCREEN } from './termio/csi.js'

class FakeTty extends EventEmitter {
  chunks: string[] = []
  columns = 20
  rows = 5
  isTTY = true

  write(chunk: string | Uint8Array, cb?: (err?: Error | null) => void): boolean {
    this.chunks.push(typeof chunk === 'string' ? chunk : Buffer.from(chunk).toString('utf8'))
    cb?.()

    return true
  }
}

const settleResize = async () => {
  await vi.advanceTimersByTimeAsync(160)
  await vi.runAllTimersAsync()
}

const makeInk = (stdout: FakeTty, stdin = new FakeTty(), stderr = new FakeTty()) =>
  new Ink({
    exitOnCtrlC: false,
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

const expectCleanRepaint = (stdout: FakeTty) => {
  const out = stdout.chunks.join('')

  expect(out).toContain(ERASE_SCREEN)
  expect(out).toContain(CURSOR_HOME)
  expect(out.indexOf(ERASE_SCREEN)).toBeLessThan(out.lastIndexOf('hello'))
}

describe('Ink resize healing', () => {
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('waits for a resize burst to settle before emitting one clean repaint', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)

    try {
      ink.setAltScreenActive(true)
      ink.render(React.createElement(Text, null, 'hello'))
      ink.onRender()
      stdout.chunks = []

      stdout.columns = 12
      stdout.rows = 8
      stdout.emit('resize')

      await vi.advanceTimersByTimeAsync(0)

      const duringResize = stdout.chunks.join('')

      expect(duringResize).not.toContain(ERASE_SCREEN)
      expect(duringResize).not.toContain('hello')

      await vi.advanceTimersByTimeAsync(159)
      expect(stdout.chunks.join('')).not.toContain(ERASE_SCREEN)

      await vi.advanceTimersByTimeAsync(1)
      await vi.runAllTimersAsync()

      expectCleanRepaint(stdout)
    } finally {
      ink.unmount()
    }
  })

  it('keeps the repaint guard active through the final React commit', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)
    let commitUpdate: ((phase: string) => void) | undefined

    const CommitUpdate = () => {
      const [phase, setPhase] = React.useState('initial')

      commitUpdate = nextPhase => setPhase(nextPhase)

      return React.createElement(Text, null, phase)
    }

    try {
      ink.setAltScreenActive(true)
      ink.render(React.createElement(CommitUpdate))
      ink.onRender()
      stdout.chunks = []

      stdout.columns = 12
      stdout.rows = 8
      stdout.emit('resize')
      await vi.advanceTimersByTimeAsync(0)

      const originalRender = ink.render.bind(ink)
      let injectCommitUpdate = true

      vi.spyOn(ink, 'render').mockImplementation(node => {
        if (injectCommitUpdate) {
          injectCommitUpdate = false
          commitUpdate?.('intermediate')
        }

        originalRender(node)
        commitUpdate?.('settled')
      })

      await vi.advanceTimersByTimeAsync(159)
      expect(stdout.chunks.join('')).not.toContain('intermediate')

      await vi.advanceTimersByTimeAsync(1)
      await vi.runAllTimersAsync()

      const out = stdout.chunks.join('')

      expect(out).toContain(ERASE_SCREEN)
      expect(out).toContain(CURSOR_HOME)
      expect(out).not.toContain('intermediate')
      expect(out).toContain('settled')
      expect(out.indexOf(ERASE_SCREEN)).toBeLessThan(out.lastIndexOf('settled'))
    } finally {
      ink.unmount()
    }
  })

  it('heals once more after a late terminal-host reflow', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)

    try {
      ink.setAltScreenActive(true)
      ink.render(React.createElement(Text, null, 'hello'))
      ink.onRender()
      stdout.chunks = []

      stdout.columns = 8
      stdout.rows = 3
      stdout.emit('resize')

      await vi.advanceTimersByTimeAsync(160)
      await vi.advanceTimersToNextTimerAsync()
      expectCleanRepaint(stdout)

      // The host can reflow its physical buffer after the fast heal. Ink has
      // no terminal-side damage signal, so model that late drift by dropping
      // the already-painted bytes and requiring one bounded follow-up heal.
      stdout.chunks = []

      await vi.advanceTimersByTimeAsync(499)
      expect(stdout.chunks.join('')).not.toContain(ERASE_SCREEN)

      await vi.advanceTimersByTimeAsync(1)
      await vi.advanceTimersByTimeAsync(40)
      expectCleanRepaint(stdout)

      const writesAfterLateHeal = stdout.chunks.length

      await vi.advanceTimersByTimeAsync(1000)
      expect(stdout.chunks).toHaveLength(writesAfterLateHeal)
    } finally {
      ink.unmount()
    }
  })

  it('resynchronizes missed terminal dimensions during a manual redraw', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)

    const TerminalSize = () => {
      const size = React.useContext(TerminalSizeContext)

      return React.createElement(Text, null, `${size?.columns}x${size?.rows}`)
    }

    try {
      ink.setAltScreenActive(true)
      ink.render(React.createElement(TerminalSize))
      ink.onRender()
      stdout.chunks = []

      // Model a terminal host that updates the PTY dimensions but drops the
      // final resize event after a large shrink/grow and a skin repaint.
      stdout.columns = 37
      stdout.rows = 11

      ink.forceRedraw()
      await vi.advanceTimersByTimeAsync(40)

      const out = stdout.chunks.join('')

      expect(out).toContain(ERASE_SCREEN)
      expect(out).toContain('37x11')
      expect(out).not.toContain('20x5')
    } finally {
      ink.unmount()
    }
  })

  it('lets a manual redraw supersede a pending resize heal', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    stdout.chunks = []

    stdout.columns = 12
    stdout.emit('resize')
    await vi.advanceTimersByTimeAsync(0)

    ink.forceRedraw()
    await vi.advanceTimersByTimeAsync(40)
    expectCleanRepaint(stdout)

    const writesAfterRedraw = stdout.chunks.length

    await vi.advanceTimersByTimeAsync(200)
    expect(stdout.chunks).toHaveLength(writesAfterRedraw)

    ink.unmount()
  })

  it('resynchronizes application listeners even when Ink already matches the PTY', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)
    let applicationColumns = 7
    const syncApplicationColumns = () => {
      applicationColumns = stdout.columns
    }

    stdout.on('resize', syncApplicationColumns)

    try {
      ink.setAltScreenActive(true)
      ink.render(React.createElement(Text, null, 'hello'))
      ink.onRender()
      stdout.chunks = []

      // Ink and stdout both remain at 20 columns, but model an application
      // listener whose coalesced state was stranded at an earlier width.
      ink.forceRedraw()

      expect(applicationColumns).toBe(20)
      expect(stdout.chunks.join('')).not.toContain(ERASE_SCREEN)

      await vi.advanceTimersByTimeAsync(40)
      expectCleanRepaint(stdout)
    } finally {
      stdout.off('resize', syncApplicationColumns)
      ink.unmount()
    }
  })

  it('lets destructive alt-screen re-entry supersede a pending resize heal', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    stdout.chunks = []

    stdout.columns = 12
    stdout.emit('resize')
    await vi.advanceTimersByTimeAsync(0)

    ink.reassertTerminalModes(true)
    await vi.advanceTimersByTimeAsync(0)

    expectCleanRepaint(stdout)

    const writesAfterReentry = stdout.chunks.length

    await vi.advanceTimersByTimeAsync(200)
    expect(stdout.chunks).toHaveLength(writesAfterReentry)

    ink.unmount()
  })

  it('heals same-dimension alt-screen resize events with an erase before repaint', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    stdout.chunks = []

    stdout.emit('resize')
    await settleResize()

    // The heal may also erase scrollback (CSI 3J interposed between 2J and H)
    // depending on which recovery path runs, so assert the invariant — screen
    // erased, then content repainted after — rather than an exact byte run.
    expectCleanRepaint(stdout)

    ink.unmount()
  })

  // Regression for issue #18449: dragging the terminal back and forth quickly
  // emits a BURST of resize events. Intermediate paints race the terminal
  // host's physical reflow and can strand stale glyphs. The burst must stay
  // quiet until it settles, then converge through one clean erase+repaint.
  it('converges to a clean erased frame after a rapid resize burst', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    stdout.chunks = []

    // Wobble the dimensions like a drag — widen, shrink, grow rows — then
    // settle back on the STARTING geometry. Even though the net dimensions are
    // unchanged, a host reflow during the burst can have scattered glyphs, so
    // the renderer must still heal rather than treat the end state as a no-op.
    const wobble: Array<[number, number]> = [
      [30, 5],
      [12, 9],
      [25, 4],
      [20, 5]
    ]

    for (const [columns, rows] of wobble) {
      stdout.columns = columns
      stdout.rows = rows
      stdout.emit('resize')
    }

    await vi.advanceTimersByTimeAsync(0)
    expect(stdout.chunks.join('')).not.toContain(ERASE_SCREEN)

    await settleResize()

    // The heal can erase scrollback too (CSI 3J interposed), so assert the
    // semantic invariant rather than an exact byte sequence: the screen was
    // erased and the content was repainted AFTER the erase — i.e. the final
    // frame is a clean repaint, not a partial diff over drifted cells.
    expectCleanRepaint(stdout)

    ink.unmount()
  })

  // The burst above ends on a same-dimension event; this isolates that worst
  // case on its own — a resize event whose dims equal the last known geometry
  // (the terminal restored the buffer / reflowed without a net size change)
  // must still arm the erase, because the physical screen may carry drift the
  // diff path cannot see (see log-update "drift repro").
  it('heals a same-dimension resize even when no React commit changes the tree', async () => {
    const stdout = new FakeTty()
    const ink = makeInk(stdout)

    ink.setAltScreenActive(true)
    ink.render(React.createElement(Text, null, 'hello'))
    ink.onRender()
    stdout.chunks = []

    // Dimensions are identical to the initial render — the tree never changes.
    stdout.emit('resize')
    await settleResize()

    expectCleanRepaint(stdout)

    ink.unmount()
  })
})
