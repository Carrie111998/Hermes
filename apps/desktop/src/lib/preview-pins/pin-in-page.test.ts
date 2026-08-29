/**
 * The engine driven the way the pane drives it — one verb per call, state
 * carried on a holder object between calls, exactly as `executeJavaScript`
 * round trips work.
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { anchorKit } from './anchor'
import { type PinCommand, pinEngineCore, pinEngineSource } from './pin-in-page'

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

  if (comment) {run({ comment, id: pin.id as string, verb: 'comment' })}

  return run({ verb: 'state' }).pins[state.pins.length - 1]
}

beforeEach(() => {
  // jsdom hands every test the same `document`, so an engine left armed by the
  // previous one is still on the capture listeners and still swallowing
  // gestures. Retire it before dropping the holder that owns its handlers.
  if (holder) {pinEngineCore(document, holder, { verb: 'hide' }, anchorKit(document))}
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

describe('hiding — closing the panel gives the page back', () => {
  it('disarms, so the next click on a link is the page\'s again', () => {
    placePin('#save', 'note')
    run({ verb: 'arm' })
    expect(run({ verb: 'hide' }).armed).toBe(false)

    const target = document.querySelector('#save')!
    let pageSawClick = false
    target.addEventListener('click', () => { pageSawClick = true })
    document.elementFromPoint = () => target
    document.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, clientX: 1, clientY: 1 }))
    const up = new MouseEvent('mouseup', { bubbles: true, cancelable: true, clientX: 1, clientY: 1 })
    document.dispatchEvent(up)
    target.dispatchEvent(new MouseEvent('click', { bubbles: true }))

    // The whole reported bug: a hidden panel that still swallows navigation.
    expect(up.defaultPrevented).toBe(false)
    expect(pageSawClick).toBe(true)
  })

  it('takes the markers down with it', () => {
    placePin('#save')
    expect(document.getElementById('hermes-pin-host')!.shadowRoot!.querySelectorAll('.pin')).toHaveLength(1)
    run({ verb: 'hide' })
    expect(document.getElementById('hermes-pin-host')!.shadowRoot!.querySelectorAll('.pin')).toHaveLength(0)
  })

  it('keeps every pin and puts them back on show', () => {
    placePin('#save', 'still here')
    run({ verb: 'hide' })
    const hidden = run({ verb: 'state' })
    expect(hidden.hidden).toBe(true)
    expect(hidden.pins).toHaveLength(1)

    const shown = run({ verb: 'show' })
    expect(shown.hidden).toBe(false)
    expect(shown.pins[0].comment).toBe('still here')
    expect(document.getElementById('hermes-pin-host')!.shadowRoot!.querySelectorAll('.pin')).toHaveLength(1)
  })

  it('unhides when armed again, so the toggle is never a dead button', () => {
    placePin('#save')
    run({ verb: 'hide' })
    const armed = run({ verb: 'arm' })
    expect(armed.hidden).toBe(false)
    expect(document.getElementById('hermes-pin-host')!.shadowRoot!.querySelectorAll('.pin')).toHaveLength(1)
  })

  it('stays quiet while hidden even if the page rebuilds under it', () => {
    placePin('#save', 'note')
    run({ verb: 'hide' })
    document.body.innerHTML = '<div id="panel"><button id="save">Save</button></div>'
    run({ verb: 'reattach' })
    expect(document.getElementById('hermes-pin-host')!.shadowRoot!.querySelectorAll('.pin')).toHaveLength(0)
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
    const ids = run({ verb: 'state' }).pins.map((pin: Record<string, unknown>) => pin.id)
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

  it('holds the click, not just the mouseup', () => {
    // preventDefault on mouseup does NOT cancel the click the browser
    // synthesises after it, so without a click listener of its own annotation
    // mode still followed links. jsdom never generates that click, which is why
    // this only surfaced in scripts/check-preview-pins.mjs — dispatch it here so
    // the fast suite keeps the listener honest.
    // A hash target, not a path: jsdom cannot navigate documents and would
    // print "Not implemented" noise over a passing run. Whether the browser
    // really follows the link is settled in check-preview-pins.mjs.
    document.body.innerHTML = '<a href="#elsewhere" id="go">Go</a>'
    const link = document.querySelector('#go')!
    document.elementFromPoint = () => link

    run({ verb: 'arm' })
    const armedClick = new MouseEvent('click', { bubbles: true, cancelable: true })
    link.dispatchEvent(armedClick)
    expect(armedClick.defaultPrevented).toBe(true)

    run({ verb: 'disarm' })
    const freeClick = new MouseEvent('click', { bubbles: true, cancelable: true })
    link.dispatchEvent(freeClick)
    expect(freeClick.defaultPrevented).toBe(false)
  })

  it('lets the comment bubble keep its own buttons', () => {
    const pin = placePin('#save')
    const root = document.getElementById('hermes-pin-host')!.shadowRoot!
    const bubble = root.querySelector('.bubble')
    expect(bubble).not.toBeNull()

    const area = bubble!.querySelector('textarea') as HTMLTextAreaElement
    area.value = 'this needs more room'
    area.dispatchEvent(new Event('input', { bubbles: true }))

    // The swallower above sees the bubble's clicks first, because the overlay
    // lives inside the document it listens on. Save has to still work.
    ;(bubble!.querySelector('button.go') as HTMLButtonElement).click()

    expect(root.querySelector('.bubble')).toBeNull()
    expect(run({ verb: 'state' }).pins.find((entry: Record<string, unknown>) => entry.id === pin.id)?.comment).toBe(
      'this needs more room'
    )
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

/**
 * The drain contract, which is all of this that jsdom can judge: it has no
 * canvas and never fires `Image.onload`, so pasting, downscaling and the strip
 * itself are only meaningful in `scripts/check-preview-pins.mjs`. What is
 * testable here is the rule those parts depend on — bytes leave the page once,
 * and never ride in a report.
 */
describe('attached images', () => {
  /** Stand in for a paste, whose async half needs a real browser. */
  function fakeShot(pinId: string, shotId: string, bytes = 'data:image/jpeg;base64,AAAA') {
    const state = holder.__hermesPinState as {
      pending: string[]
      pins: Record<string, unknown>[]
      shotData: Record<string, string>
    }

    const pin = state.pins.find(entry => entry.id === pinId)!
    pin.shots = ((pin.shots as unknown[]) ?? []).concat([
      { h: 40, id: shotId, thumb: 'data:image/jpeg;base64,tiny', w: 60 }
    ])
    state.shotData[shotId] = bytes
    state.pending.push(shotId)

    return state
  }

  it('advertises what it is holding on every report, armed or not', () => {
    const pin = placePin('#save', 'like this')
    fakeShot(pin.id as string, 's1')

    // Not only while annotating: an image pasted and then abandoned still has
    // to get out before the page goes away.
    expect(run({ verb: 'state' }).pendingShots).toEqual(['s1'])
    expect(run({ verb: 'hide' }).pendingShots).toEqual(['s1'])
  })

  it('hands the bytes over once and forgets them', () => {
    const pin = placePin('#save')
    fakeShot(pin.id as string, 's1', 'data:image/jpeg;base64,PAYLOAD')

    const first = run({ id: 's1', verb: 'take' })
    expect(first.shot).toBe('data:image/jpeg;base64,PAYLOAD')
    expect(first.pendingShots).toEqual([])

    // Asked twice — a re-render, a double poll — it must not resurrect them.
    expect(run({ id: 's1', verb: 'take' }).shot).toBeNull()
  })

  it('never carries the bytes in an ordinary read', () => {
    const pin = placePin('#save')
    fakeShot(pin.id as string, 's1', 'data:image/jpeg;base64,PAYLOAD')

    const state = run({ verb: 'state' })
    // A megabyte of base64 crossing the bridge every 700ms is the difference
    // between a panel that feels live and one that stutters.
    expect(state.shot).toBeNull()
    expect(JSON.stringify(state.pins)).not.toContain('PAYLOAD')
    expect(JSON.stringify(state.pins)).toContain('tiny')
  })

  it('keeps the thumbnail on the pin, so the list can show it', () => {
    const pin = placePin('#save')
    fakeShot(pin.id as string, 's1')
    const shots = run({ verb: 'state' }).pins[0].shots as { thumb: string }[]
    expect(shots).toHaveLength(1)
    expect(shots[0].thumb).toContain('data:image/jpeg')
  })

  it('drops undrained bytes along with the pin that owned them', () => {
    const pin = placePin('#save')
    const state = fakeShot(pin.id as string, 's1')
    run({ id: pin.id as string, verb: 'remove' })
    expect(state.shotData.s1).toBeUndefined()
  })

  it('drops them all on clear', () => {
    const first = placePin('#save')
    const second = placePin('#note')
    const state = fakeShot(first.id as string, 's1')
    fakeShot(second.id as string, 's2')

    run({ verb: 'clear' })
    expect(Object.keys(state.shotData)).toHaveLength(0)
    expect(run({ verb: 'state' }).pendingShots).toEqual([])
  })

  it('marks a marker that carries one, so it is visible without opening it', () => {
    const pin = placePin('#save')
    fakeShot(pin.id as string, 's1')
    // A real paste repaints as soon as the thumbnail lands; `state` is the read
    // side and deliberately does not, so that polling stays cheap.
    run({ verb: 'show' })
    const marker = document.getElementById('hermes-pin-host')!.shadowRoot!.querySelector('.pin')
    expect(marker?.className).toContain('shot')
  })

  it('survives a seeded state that predates images', () => {
    // A pin book written before this existed has no shotData and no pending.
    holder = { __hermesPinState: { armed: false, drag: null, pins: [], seq: 0 } }
    expect(() => run({ verb: 'state' })).not.toThrow()
    expect(run({ verb: 'state' }).pendingShots).toEqual([])
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
     
    const engine = eval(source)
    const state = engine(document, {}, { verb: 'state' })
    expect(state.armed).toBe(false)
    expect(state.pins).toEqual([])
  })

  it('works end to end once round-tripped through a string', () => {
     
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
