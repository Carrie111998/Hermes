/**
 * PIN ENGINE — annotation mode, inside the guest page.
 *
 * Parked on a window global and driven by verbs over `executeJavaScript`, the
 * same channel `preview-tour.ts` and `preview-act.ts` use. It has to live in the
 * page rather than float over the pane because a pin follows its element
 * through scrolls and reflows, and only the page knows where its elements are.
 *
 * The overlay is built inside a shadow root so page CSS cannot restyle it and
 * page selectors cannot find it — a stray `* { display: none }` in the app
 * being reviewed should not be able to hide the review tools.
 *
 * SELF-CONTAINMENT. `pinEngineSource` stringifies the factories below into one
 * expression. Module scope does not exist in the guest page, so `pinEngineCore`
 * receives its dependencies as arguments rather than importing them, and every
 * helper it uses is declared inside its own body. See `preview-act/naming.ts`
 * for the full contract and the failure mode.
 */

import { anchorKit, type AnchorKit } from './anchor'

/** Verbs the app can send. `state` is the read side; the rest mutate. */
export type PinVerb =
  | 'arm'
  | 'disarm'
  | 'hide'
  | 'show'
  | 'state'
  | 'reattach'
  | 'comment'
  | 'resolve'
  | 'remove'
  | 'clear'

export interface PinCommand {
  comment?: string
  id?: string
  verb: PinVerb
}

/**
 * The engine body.
 *
 * `holder` is the window object the engine and its pins live on, so state
 * survives between calls; `kit` is the stringified anchor factory, already
 * built against this document.
 */
export function pinEngineCore(doc: Document, holder: Record<string, unknown>, command: PinCommand, kit: AnchorKit) {
  const STATE_KEY = '__hermesPinState'
  const now = () => Date.now()

  const state = (holder[STATE_KEY] as Record<string, unknown> | undefined) ?? {
    armed: false,
    drag: null,
    hidden: false,
    pins: [],
    seq: 0
  }

  holder[STATE_KEY] = state

  const pins = state.pins as Record<string, unknown>[]

  // ---- overlay -----------------------------------------------------------

  const HOST_ID = 'hermes-pin-host'

  const host = () => {
    let node = doc.getElementById(HOST_ID) as HTMLElement | null

    if (node && node.shadowRoot) {return node}
    node = doc.createElement('div')
    node.id = HOST_ID
    // Fixed and non-interactive by default: the overlay must not eat clicks
    // when annotation mode is off, or the page becomes unusable the moment the
    // engine has been injected once.
    node.setAttribute(
      'style',
      'position:fixed;inset:0;z-index:2147483646;pointer-events:none;'
    )
    const root = node.attachShadow({ mode: 'open' })
    const style = doc.createElement('style')
    style.textContent = [
      '.hl{position:fixed;border:2px solid #d99a5b;background:rgba(217,154,91,.14);',
      'border-radius:3px;pointer-events:none;transition:all .04s linear}',
      '.pin{position:fixed;width:22px;height:22px;border-radius:50% 50% 50% 2px;',
      'background:#d99a5b;color:#1c1b19;font:600 12px/22px system-ui;text-align:center;',
      'pointer-events:auto;cursor:pointer;box-shadow:0 1px 4px rgba(0,0,0,.4);transform:translate(-4px,-26px)}',
      '.pin.resolved{background:#7fc08e}',
      '.pin.orphan{background:#8a8a8a}',
      '.box{position:fixed;border:2px dashed #d99a5b;background:rgba(217,154,91,.1);pointer-events:none}',
      '.bubble{position:fixed;width:250px;background:#1e1d1a;color:#eae7e1;border:1px solid #3a3733;',
      'border-radius:8px;padding:9px;pointer-events:auto;box-shadow:0 4px 18px rgba(0,0,0,.5);',
      'font:13px/1.45 system-ui}',
      '.bubble textarea{width:100%;min-height:54px;background:#161513;color:#eae7e1;',
      'border:1px solid #3a3733;border-radius:5px;padding:6px;font:13px system-ui;resize:vertical}',
      '.bubble .row{display:flex;gap:6px;margin-top:7px}',
      '.bubble button{flex:1;padding:5px;border:1px solid #3a3733;border-radius:5px;',
      'background:#242220;color:#eae7e1;font:600 12px system-ui;cursor:pointer}',
      '.bubble button.go{background:#d99a5b;color:#1c1b19;border-color:#d99a5b}'
    ].join('')
    root.append(style)
    doc.body.append(node)

    return node
  }

  const shadow = () => host().shadowRoot as ShadowRoot

  const clearLayer = (selector: string) => {
    for (const node of Array.from(shadow().querySelectorAll(selector))) {node.remove()}
  }

  const fractionToBox = (rect: { h: number; w: number; x: number; y: number }) => {
    const view = doc.defaultView
    const width = Math.max(1, doc.documentElement.scrollWidth)
    const height = Math.max(1, doc.documentElement.scrollHeight)

    return {
      height: rect.h * height,
      left: rect.x * width - (view ? view.scrollX : 0),
      top: rect.y * height - (view ? view.scrollY : 0),
      width: rect.w * width
    }
  }

  /** Redraw every pin marker where its element currently is. */
  const paint = () => {
    clearLayer('.pin')

    // Hidden means hidden. The user closed the panel to get their page back,
    // and leaving markers floating over it is the same complaint again.
    if (state.hidden) {return}
    const root = shadow()
    pins.forEach((pin, index) => {
      const marker = doc.createElement('div')
      marker.className = 'pin' + (pin.resolved ? ' resolved' : '') + (pin.orphaned ? ' orphan' : '')
      marker.textContent = String(index + 1)
      marker.dataset.pin = String(pin.id)

      let box: { height: number; left: number; top: number; width: number } | null = null

      if (pin.kind === 'element' && pin.anchor) {
        const match = kit.resolve(pin.anchor as never)

        if (match.element) {
          const live = match.element.getBoundingClientRect()
          box = { height: live.height, left: live.left, top: live.top, width: live.width }
        }
      } else if (pin.region) {
        box = fractionToBox(pin.region as never)
      }

      if (!box) {
        // An orphan has nowhere to sit. Keeping it off-screen rather than at
        // 0,0 avoids a pile of grey markers in the corner that look like a bug.
        return
      }

      marker.style.left = box.left + 'px'
      marker.style.top = box.top + 'px'
      root.append(marker)
    })
  }

  const closeBubble = () => clearLayer('.bubble')

  const openBubble = (pinId: string, left: number, top: number) => {
    closeBubble()
    const pin = pins.find(entry => entry.id === pinId)

    if (!pin) {return}
    const view = doc.defaultView
    const bubble = doc.createElement('div')
    bubble.className = 'bubble'
    // Keep the bubble on screen when the pin is near the right or bottom edge.
    const maxLeft = (view ? view.innerWidth : 800) - 262
    const maxTop = (view ? view.innerHeight : 600) - 140
    bubble.style.left = Math.max(8, Math.min(left, maxLeft)) + 'px'
    bubble.style.top = Math.max(8, Math.min(top, maxTop)) + 'px'

    const area = doc.createElement('textarea')
    area.value = String(pin.comment || '')
    area.placeholder = 'What should change here?'
    const row = doc.createElement('div')
    row.className = 'row'
    const save = doc.createElement('button')
    save.className = 'go'
    save.textContent = 'Save'
    const drop = doc.createElement('button')
    drop.textContent = 'Delete'

    save.addEventListener('click', event => {
      event.stopPropagation()
      pin.comment = area.value
      closeBubble()
      paint()
    })
    drop.addEventListener('click', event => {
      event.stopPropagation()
      const index = pins.findIndex(entry => entry.id === pinId)

      if (index !== -1) {pins.splice(index, 1)}
      closeBubble()
      paint()
    })

    // Typing in the page must not reach the page. A review comment containing
    // "d" should not trigger the app's own keyboard shortcut for it.
    for (const type of ['keydown', 'keyup', 'keypress']) {
      area.addEventListener(type, event => event.stopPropagation())
    }

    row.append(save, drop)
    bubble.append(area, row)
    shadow().append(bubble)
    area.focus()
  }

  // ---- annotation mode ---------------------------------------------------

  const targetAt = (x: number, y: number): Element | null => {
    // The host is pointer-events:none at the root, but a marker inside it is
    // not, so ask the page what is under the cursor with the host discounted.
    const found = doc.elementFromPoint(x, y)

    if (!found || found.id === HOST_ID) {return null}

    return found
  }

  const onMove = (event: MouseEvent) => {
    if (!state.armed) {return}

    if (state.drag) {
      const drag = state.drag as { x0: number; y0: number }
      clearLayer('.box')
      const box = doc.createElement('div')
      box.className = 'box'
      box.style.left = Math.min(drag.x0, event.clientX) + 'px'
      box.style.top = Math.min(drag.y0, event.clientY) + 'px'
      box.style.width = Math.abs(event.clientX - drag.x0) + 'px'
      box.style.height = Math.abs(event.clientY - drag.y0) + 'px'
      shadow().append(box)

      return
    }

    clearLayer('.hl')
    const el = targetAt(event.clientX, event.clientY)

    if (!el) {return}
    const rect = el.getBoundingClientRect()
    const highlight = doc.createElement('div')
    highlight.className = 'hl'
    highlight.style.left = rect.left + 'px'
    highlight.style.top = rect.top + 'px'
    highlight.style.width = rect.width + 'px'
    highlight.style.height = rect.height + 'px'
    shadow().append(highlight)
  }

  const onDown = (event: MouseEvent) => {
    if (!state.armed) {return}
    state.drag = { x0: event.clientX, y0: event.clientY }
  }

  const addPin = (entry: Record<string, unknown>) => {
    state.seq = (state.seq as number) + 1
    entry.id = 'pin-' + state.seq + '-' + now().toString(36)
    entry.createdAt = now()
    entry.resolved = false
    entry.pageUrl = doc.location ? doc.location.href : ''
    pins.push(entry)

    return entry.id as string
  }

  const onUp = (event: MouseEvent) => {
    if (!state.armed) {return}
    const drag = state.drag as { x0: number; y0: number } | null
    state.drag = null
    clearLayer('.box')

    if (!drag) {return}

    // Suppress the page's own click. Annotation mode is a review overlay, not a
    // way to accidentally submit the form you are commenting on.
    event.preventDefault()
    event.stopPropagation()

    const dx = Math.abs(event.clientX - drag.x0)
    const dy = Math.abs(event.clientY - drag.y0)
    const view = doc.defaultView
    const width = Math.max(1, doc.documentElement.scrollWidth)
    const height = Math.max(1, doc.documentElement.scrollHeight)

    let id: string

    if (dx > 6 || dy > 6) {
      // A drag: a region, for images, charts and canvases where no node means
      // what the user is pointing at.
      const left = Math.min(drag.x0, event.clientX) + (view ? view.scrollX : 0)
      const top = Math.min(drag.y0, event.clientY) + (view ? view.scrollY : 0)
      id = addPin({
        comment: '',
        kind: 'region',
        region: { h: dy / height, w: dx / width, x: left / width, y: top / height },
        target: Math.round(dx) + '×' + Math.round(dy) + ' region'
      })
    } else {
      const el = targetAt(event.clientX, event.clientY)

      if (!el) {return}
      const anchor = kit.capture(el)
      id = addPin({
        anchor,
        comment: '',
        kind: 'element',
        matchedBy: 'placed',
        target: anchor.label || anchor.role
      })
    }

    clearLayer('.hl')
    paint()
    openBubble(id, event.clientX + 14, event.clientY + 14)
  }

  const onKey = (event: KeyboardEvent) => {
    if (event.key === 'Escape' && state.armed) {
      if (shadow().querySelector('.bubble')) {closeBubble()}
      else {disarm()}
    }
  }

  const onPinClick = (event: MouseEvent) => {
    const target = event.target as HTMLElement | null
    const id = target && target.dataset ? target.dataset.pin : null

    if (!id) {return}
    event.preventDefault()
    event.stopPropagation()
    openBubble(id, event.clientX + 14, event.clientY + 14)
  }

  const onScroll = () => paint()

  const arm = () => {
    if (state.armed) {return}
    state.armed = true
    // Arming is a request to see what you are annotating.
    state.hidden = false
    host().setAttribute(
      'style',
      'position:fixed;inset:0;z-index:2147483646;pointer-events:none;cursor:crosshair;'
    )
    doc.documentElement.style.cursor = 'crosshair'
    // Capture phase, so the page cannot swallow the gesture before we see it.
    doc.addEventListener('mousemove', onMove, true)
    doc.addEventListener('mousedown', onDown, true)
    doc.addEventListener('mouseup', onUp, true)
    doc.addEventListener('keydown', onKey, true)
    const view = doc.defaultView

    if (view) {
      view.addEventListener('scroll', onScroll, true)
      view.addEventListener('resize', onScroll, true)
    }

    state.handlers = { onDown, onKey, onMove, onScroll, onUp }
    paint()
  }

  const disarm = () => {
    if (!state.armed) {return}
    state.armed = false
    state.drag = null
    doc.documentElement.style.cursor = ''
    const handlers = state.handlers as Record<string, EventListener> | undefined

    if (handlers) {
      doc.removeEventListener('mousemove', handlers.onMove, true)
      doc.removeEventListener('mousedown', handlers.onDown, true)
      doc.removeEventListener('mouseup', handlers.onUp, true)
      doc.removeEventListener('keydown', handlers.onKey, true)
      const view = doc.defaultView

      if (view) {
        view.removeEventListener('scroll', handlers.onScroll, true)
        view.removeEventListener('resize', handlers.onScroll, true)
      }
    }

    clearLayer('.hl')
    clearLayer('.box')
    closeBubble()
    paint()
  }

  /**
   * Put the page back the way the user found it, without losing anything.
   *
   * Closing the panel has to be a full retreat: disarmed, no markers, no
   * highlight, cursor back to normal. Anything less and the next click on a
   * link is swallowed by a review overlay the user believes they dismissed —
   * which is exactly the trap they hit.
   */
  const hide = () => {
    disarm()
    state.hidden = true
    clearLayer('.pin')
    closeBubble()
  }

  const show = () => {
    state.hidden = false
    paint()
  }

  /**
   * Re-run the ladder over every pin.
   *
   * Called after a navigation or reload. A pin whose element is gone is marked
   * orphaned and kept — the comment is the user's writing, and throwing it away
   * because a build changed the DOM would lose real work.
   */
  const reattach = () => {
    for (const pin of pins) {
      if (pin.kind !== 'element' || !pin.anchor) {continue}
      const match = kit.resolve(pin.anchor as never)
      pin.orphaned = !match.element
      pin.matchedBy = match.how

      if (match.element) {
        // Re-capture from the element we just found, so the anchor tracks the
        // page forward instead of decaying against the version it was placed on.
        pin.anchor = kit.capture(match.element)
      }
    }

    paint()
  }

  // Markers stay clickable even when disarmed, so a comment can be reopened
  // without re-entering annotation mode. Bound once per page.
  if (!state.wired) {
    shadow().addEventListener('click', onPinClick as EventListener, true)
    state.wired = true
  }

  switch (command.verb) {
    case 'arm':
      arm()

      break

    case 'disarm':
      disarm()

      break

    case 'hide':
      hide()

      break

    case 'show':
      show()

      break

    case 'reattach':
      reattach()

      break
    case 'comment': {
      const pin = pins.find(entry => entry.id === command.id)

      if (pin) {pin.comment = String(command.comment ?? '')}

      break
    }

    case 'resolve': {
      const pin = pins.find(entry => entry.id === command.id)

      if (pin) {pin.resolved = !pin.resolved}
      paint()

      break
    }

    case 'remove': {
      const index = pins.findIndex(entry => entry.id === command.id)

      if (index !== -1) {pins.splice(index, 1)}
      paint()

      break
    }

    case 'clear':
      pins.splice(0, pins.length)
      closeBubble()
      paint()

      break

    case 'state':

    default:
      break
  }

  return {
    armed: state.armed === true,
    hidden: state.hidden === true,
    pins: JSON.parse(JSON.stringify(pins)),
    url: doc.location ? doc.location.href : ''
  }
}

/** One injectable expression: the anchor factory, then the engine over it. */
export function pinEngineSource(): string {
  return `(function (doc, holder, command) {
  var kit = (${anchorKit.toString()})(doc);
  return (${pinEngineCore.toString()})(doc, holder, command, kit);
})`
}
