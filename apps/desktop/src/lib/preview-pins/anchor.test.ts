/**
 * The tests that decide whether pins are a feature or a demo.
 *
 * Every case below is a page being rebuilt under a pin that was already placed:
 * a plain reload, a framework re-render that drops ids, a list that grew a row
 * above the pinned one, a node that genuinely went away. A pin that cannot
 * survive these is a pin the user has to re-place after every save, which is
 * the same as not having pins.
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { ANCHOR_MIN_CONFIDENCE, anchorKit } from './anchor'

/** Replace the document body, the way a reload or a re-render does. */
function render(html: string) {
  document.body.innerHTML = html

  return anchorKit(document)
}

beforeEach(() => {
  document.body.innerHTML = ''
})

describe('capture', () => {
  it('prefers the page\'s own identity when it offers one', () => {
    const kit = render('<button id="save">Save</button>')
    const anchor = kit.capture(document.querySelector('#save')!)
    expect(anchor.selector).toBe('#save')
    expect(anchor.role).toBe('button')
    expect(anchor.label).toBe('Save')
  })

  it('falls back to data-testid before giving up on a selector', () => {
    const kit = render('<button data-testid="save-btn">Save</button>')
    expect(kit.capture(document.querySelector('button')!).selector).toBe('[data-testid="save-btn"]')
  })

  it('records an empty selector rather than inventing one', () => {
    const kit = render('<button>Save</button>')
    expect(kit.capture(document.querySelector('button')!).selector).toBe('')
  })

  it('roots the path at the nearest identified ancestor', () => {
    const kit = render('<div id="panel"><section><button>Go</button></section></div>')
    const anchor = kit.capture(document.querySelector('button')!)
    // Rooted at #panel, so anything changing above it cannot break the path.
    expect(anchor.path).toBe('#panel>section>button')
  })

  it('numbers same-tag siblings so the path is unambiguous', () => {
    const kit = render('<div id="p"><span>a</span><span>b</span></div>')
    const anchor = kit.capture(document.querySelectorAll('span')[1])
    expect(anchor.path).toBe('#p>span:nth-of-type(2)')
  })

  it('records which of several identical elements it was', () => {
    const kit = render('<ul id="l"><li>Row</li><li>Row</li><li>Row</li></ul>')
    expect(kit.capture(document.querySelectorAll('li')[2]).ordinal).toBe(2)
  })
})

describe('resolve — the page came back', () => {
  it('finds it again after an identical reload', () => {
    const kit = render('<div id="panel"><button id="save">Save</button></div>')
    const anchor = kit.capture(document.querySelector('#save')!)

    const reloaded = render('<div id="panel"><button id="save">Save</button></div>')
    const match = reloaded.resolve(anchor)

    expect(match.how).toBe('selector')
    expect(match.confidence).toBe(1)
    expect((match.element as HTMLElement).textContent).toBe('Save')
  })

  it('survives a re-render that dropped every id', () => {
    const kit = render('<div id="panel"><button id="save">Save changes</button></div>')
    const anchor = kit.capture(document.querySelector('#save')!)

    // React remounting with generated ids gone is the commonest shape of this.
    const rerendered = render('<div id="panel"><button>Save changes</button></div>')
    const match = rerendered.resolve(anchor)

    expect(match.element).not.toBeNull()
    expect(match.confidence).toBeGreaterThanOrEqual(ANCHOR_MIN_CONFIDENCE)
    expect((match.element as HTMLElement).textContent).toBe('Save changes')
  })

  it('survives markup being rearranged above the pinned element', () => {
    const kit = render('<main><button>Publish</button></main>')
    const anchor = kit.capture(document.querySelector('button')!)

    // The path is now wrong, but role+label is unique, so it still lands.
    const moved = render('<div><aside>nav</aside><main><section><button>Publish</button></section></main></div>')
    const match = moved.resolve(anchor)

    expect(match.how).toBe('role+label')
    expect((match.element as HTMLElement).textContent).toBe('Publish')
  })

  it('holds its place when a row is inserted above it in a list', () => {
    const kit = render('<ul id="l"><li><button>Edit</button></li><li><button>Edit</button></li></ul>')
    const anchor = kit.capture(document.querySelectorAll('button')[1])
    expect(anchor.ordinal).toBe(1)

    const grown = render(
      '<ul id="l"><li><button>Edit</button></li><li><button>Edit</button></li><li><button>Edit</button></li></ul>'
    )

    const match = grown.resolve(anchor)

    // Ambiguous by label, so the ordinal decides — the same rung the user's
    // intent lives on when every row looks identical.
    expect(match.element).toBe(document.querySelectorAll('button')[1])
    expect(match.confidence).toBeGreaterThanOrEqual(ANCHOR_MIN_CONFIDENCE)
  })

  it('tolerates a label that gained a suffix', () => {
    const kit = render('<div id="p"><button>Inbox</button></div>')
    const anchor = kit.capture(document.querySelector('button')!)

    const withCount = render('<div id="p"><button>Inbox 3</button></div>')
    const match = withCount.resolve(anchor)

    expect(match.element).not.toBeNull()
    expect(match.how).toBe('path+label')
  })

  it('finds an unlabelled element by its text', () => {
    const kit = render('<div id="p"><section><span>Total revenue</span></section></div>')
    const anchor = kit.capture(document.querySelector('span')!)

    // Structure changed and there is no role/label to speak of; text is all
    // that is left, and it is unique.
    const moved = render('<article><div><span>Total revenue</span></div></article>')
    const match = moved.resolve(anchor)

    expect(match.element).not.toBeNull()
    expect(match.confidence).toBeGreaterThanOrEqual(ANCHOR_MIN_CONFIDENCE)
  })
})

describe('resolve — refusing to guess', () => {
  it('orphans a pin whose element is gone rather than taking a neighbour', () => {
    const kit = render('<div id="p"><button>Delete account</button></div>')
    const anchor = kit.capture(document.querySelector('button')!)

    const without = render('<div id="p"><button>Cancel</button></div>')
    const match = without.resolve(anchor)

    // The dangerous outcome would be attaching "are you sure about this?" to
    // Cancel. Orphaning makes the user re-place it; guessing would hand the
    // agent a comment about the wrong control.
    expect(match.element).toBeNull()
    expect(match.how).toBe('orphaned')
    expect(match.confidence).toBeLessThan(ANCHOR_MIN_CONFIDENCE)
  })

  it('orphans rather than matching on position alone', () => {
    const kit = render('<div id="p"><button>Approve</button></div>')
    const anchor = kit.capture(document.querySelector('button')!)

    // Same slot, same shape, different meaning. Geometry would happily match.
    const replaced = render('<div id="p"><button>Reject</button></div>')
    expect(replaced.resolve(anchor).element).toBeNull()
  })

  it('orphans on an empty page', () => {
    const kit = render('<button id="go">Go</button>')
    const anchor = kit.capture(document.querySelector('button')!)
    expect(render('').resolve(anchor).element).toBeNull()
  })

  it('treats an unparseable stored selector as a miss, not a crash', () => {
    const kit = render('<button>Go</button>')
    const anchor = kit.capture(document.querySelector('button')!)
    expect(() => kit.resolve({ ...anchor, selector: '#((((' })).not.toThrow()
    // Still found — the ladder moved past the broken rung.
    expect(kit.resolve({ ...anchor, selector: '#((((' }).element).not.toBeNull()
  })

  it('reports how it matched so a weak match is visible in the UI', () => {
    const kit = render('<button id="x">Go</button>')
    const anchor = kit.capture(document.querySelector('button')!)
    expect(kit.resolve(anchor).how).toBe('selector')
    expect(render('<button>Go</button>').resolve(anchor).how).toBe('role+label')
  })
})

describe('self-containment', () => {
  it('stringifies without capturing a single free identifier', () => {
    // The factory is injected into the guest page by pin-in-page.ts, where
    // module scope does not exist. A reference to anything outside its own body
    // is a ReferenceError that Electron reports only as "Script failed to
    // execute" — so assert the shape that prevents it.
    const source = anchorKit.toString()
    expect(source.startsWith('function anchorKit')).toBe(true)

    for (const external of ['ANCHOR_MIN_CONFIDENCE', 'import(', 'require(']) {
      expect(source.includes(external)).toBe(false)
    }
  })

  it('is a live function once round-tripped through a string', () => {
    render('<button id="save">Save</button>')
     
    const rebuilt = eval(`(${anchorKit.toString()})`)(document)
    const anchor = rebuilt.capture(document.querySelector('#save'))
    expect(rebuilt.resolve(anchor).confidence).toBe(1)
  })
})
