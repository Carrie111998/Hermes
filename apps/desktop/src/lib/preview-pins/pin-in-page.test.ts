/**
 * The engine driven the way the pane drives it — one verb per call, state
 * carried on a holder object between calls, exactly as `executeJavaScript`
 * round trips work.
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { anchorKit } from './anchor'
import { pinEngineCore, pinEngineSource, type PinCommand } from './pin-in-page'

let holder: Record<string, unknown>

function run(command: PinCommand) {
  return pinEngineCore(document, holder, command, anchorKit(document))
}

/** Place a pin the way a user does: arm, press, release over an element. */
function placePin(selector: string, comment = '') {
  const target = document.querySelector(selector)!
  const rect = target.getBoundingClientRect()
  const x = rect.left + 1
  const y = rect.top + 1
  // jsdom has no layout, so elementFromPoint needs help to name our target.
  document.elementFromPoint = () => target
  run({ verb: 'arm' })
  document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: x, clientY: y }))
  document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: x, clientY: y }))
  const state = run({ verb: 'state' })
  const pin = state.pins[state.pins.length - 1]
  if (comment) run({ comment, id: pin.id as string, verb: 'comment' })
  return run({ verb: 'state' }).pins[state.pins.length - 1]
}

beforeEach(() => {
  holder = {}
  document.body.innerHTML = '<div id="panel"><button id="save">Save</button><p id="note">A note</p></div>'
})

describe('arming', () => {
  it('starts disarmed and reports it', () => {
    expect(run({ verb: 'state' }).armed).toBe(false)
  })

  it('arms and disarms', () => {
    expect(run({ verb: 'arm' }).armed).toBe(true)
    expect(run({ verb: 'disarm' }).armed).toBe(false)
  })

  it('builds its overlay inside a shadow root so page CSS cannot reach it', () => {
    run({ verb: 'arm' })
    const host = document.getElementById('hermes-pin-host')
    expect(host).not.toBeNull()
    expect(host!.shadowRoot).not.toBeNull()
    // A page-wide selector must not be able to find, restyle or hide the
    // review tools of the app being reviewed.
    expect(document.querySelectorAll('.pin, .bubble, .hl').length).toBe(0)
  })

  it('leaves the page clickable when disarmed', () => {
    run({ verb: 'arm' })
    run({ verb: 'disarm' })
    const style = document.getElementById('hermes-pin-host')!.getAttribute('style') ?? ''
    expect(style).toContain('pointer-events:none')
  })
})

describe('placing pins', () => {
  it('captures an anchor for the clicked element', () => {
    const pin = placePin('#save', 'too tight here')
    expect(pin.kind).toBe('element')
    expect((pin.anchor as { selector: string }).selector).toBe('#save')
    expect(pin.comment).toBe('too tight here')
    expect(pin.resolved).toBe(false)
  })

  it('records the page it was placed on', () => {
    expect(placePin('#save').pageUrl).toBe(document.location.href)
  })

  it('gives every pin a distinct id', () => {
    placePin('#save')
    placePin('#note')
    const ids = run({ verb: 'state' }).pins.map(pin => pin.id)
    expect(new Set(ids).size).toBe(2)
  })

  it('swallows the gesture so the page does not act on it', () => {
    const target = document.querySelector('#save')!
    document.elementFromPoint = () => target
    let pageSawClick = false
    target.addEventListener('click', () => { pageSawClick = true })

    run({ verb: 'arm' })
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 1, clientY: 1 }))
    const up = new MouseEvent('mouseup', { bubbles: true, cancelable: true, clientX: 1, clientY: 1 })
    document.dispatchEvent(up)

    // Commenting on a Submit button must not submit the form.
    expect(up.defaultPrevented).toBe(true)
    expect(pageSawClick).toBe(false)
  })

  it('makes a region pin from a drag, for things that are not elements', () => {
    document.elementFromPoint = () => document.querySelector('#save')
    run({ verb: 'arm' })
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 10, clientY: 10 }))
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 90, clientY: 70 }))

    const pin = run({ verb: 'state' }).pins[0]
    expect(pin.kind).toBe('region')
    expect(pin.region).toBeTruthy()
    expect(pin.anchor).toBeUndefined()
  })

  it('does nothing on a stray click over nothing', () => {
    document.elementFromPoint = () => null
    run({ verb: 'arm' })
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 5, clientY: 5 }))
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 5, clientY: 5 }))
    expect(run({ verb: 'state' }).pins).toHaveLength(0)
  })

  it('ignores gestures once disarmed', () => {
    placePin('#save')
    run({ verb: 'disarm' })
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 1, clientY: 1 }))
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 1, clientY: 1 }))
    expect(run({ verb: 'state' }).pins).toHaveLength(1)
  })
})

describe('managing pins', () => {
  it('toggles resolved', () => {
    const pin = placePin('#save', 'fix this')
    expect(run({ id: pin.id as string, verb: 'resolve' }).pins[0].resolved).toBe(true)
    expect(run({ id: pin.id as string, verb: 'resolve' }).pins[0].resolved).toBe(false)
  })

  it('removes one pin', () => {
    const first = placePin('#save')
    placePin('#note')
    expect(run({ id: first.id as string, verb: 'remove' }).pins).toHaveLength(1)
  })

  it('clears them all', () => {
    placePin('#save')
    placePin('#note')
    expect(run({ verb: 'clear' }).pins).toHaveLength(0)
  })

  it('edits a comment without touching the anchor', () => {
    const pin = placePin('#save', 'first')
    const after = run({ comment: 'second', id: pin.id as string, verb: 'comment' })
    expect(run({ verb: 'state' }).pins[0].comment).toBe('second')
    expect(after.pins[0].anchor).toEqual(pin.anchor)
  })
})

describe('surviving a rebuild', () => {
  it('re-attaches pins after the page is rebuilt', () => {
    placePin('#save', 'too much padding')

    // The app re-rendered: same button, id gone.
    document.body.innerHTML = '<div id="panel"><button>Save</button></div>'
    const state = run({ verb: 'reattach' })

    expect(state.pins[0].orphaned).toBe(false)
    expect(state.pins[0].comment).toBe('too much padding')
    expect(state.pins[0].matchedBy).toBeTruthy()
  })

  it('keeps the comment but marks the pin orphaned when the element is gone', () => {
    placePin('#save', 'this button is wrong')

    document.body.innerHTML = '<div id="panel"><p>nothing here now</p></div>'
    const state = run({ verb: 'reattach' })

    // The user's sentence is real work. Losing it because a build changed the
    // DOM would be worse than showing it detached.
    expect(state.pins[0].orphaned).toBe(true)
    expect(state.pins[0].comment).toBe('this button is wrong')
  })

  it('re-captures the anchor from the element it just found', () => {
    placePin('#save')
    document.body.innerHTML = '<main><section><button>Save</button></section></main>'
    const state = run({ verb: 'reattach' })

    // Tracking forward, not decaying against the version it was placed on.
    expect((state.pins[0].anchor as { path: string }).path).toContain('section')
    expect((state.pins[0].anchor as { selector: string }).selector).toBe('')
  })

  it('leaves region pins alone — they were never bound to an element', () => {
    document.elementFromPoint = () => document.querySelector('#save')
    run({ verb: 'arm' })
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 10, clientY: 10 }))
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 90, clientY: 70 }))
    const before = run({ verb: 'state' }).pins[0].region

    document.body.innerHTML = '<p>totally different</p>'
    const after = run({ verb: 'reattach' }).pins[0]
    expect(after.region).toEqual(before)
    expect(after.orphaned).toBeUndefined()
  })
})

describe('injectable source', () => {
  it('assembles into one evaluable expression', () => {
    const source = pinEngineSource()
    expect(source.startsWith('(function (doc, holder, command)')).toBe(true)
    // eslint-disable-next-line no-eval
    const engine = eval(source)
    const state = engine(document, {}, { verb: 'state' })
    expect(state.armed).toBe(false)
    expect(state.pins).toEqual([])
  })

  it('works end to end once round-tripped through a string', () => {
    // eslint-disable-next-line no-eval
    const engine = eval(pinEngineSource())
    const box: Record<string, unknown> = {}
    document.elementFromPoint = () => document.querySelector('#save')
    engine(document, box, { verb: 'arm' })
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 1, clientY: 1 }))
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 1, clientY: 1 }))
    expect(engine(document, box, { verb: 'state' }).pins).toHaveLength(1)
  })

  it('carries no free identifier into the guest page', () => {
    const source = pinEngineSource()
    expect(source.includes('import(')).toBe(false)
    expect(source.includes('require(')).toBe(false)

    // The real proof, and the only one worth having: `new Function` compiles in
    // global scope with no access to this module, so anything the engine did
    // not bring with it is a ReferenceError here — which is exactly what the
    // guest page would throw, except there it arrives as an unhelpful "Script
    // failed to execute". `eval` cannot prove this: it can see anchorKit
    // through the test file's own scope and would pass a broken build.
    const engine = new Function(`return ${source}`)()
    document.elementFromPoint = () => document.querySelector('#save')
    const box: Record<string, unknown> = {}
    expect(() => engine(document, box, { verb: 'arm' })).not.toThrow()
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 1, clientY: 1 }))
    document.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, clientX: 1, clientY: 1 }))
    expect(engine(document, box, { verb: 'state' }).pins).toHaveLength(1)
  })
})
