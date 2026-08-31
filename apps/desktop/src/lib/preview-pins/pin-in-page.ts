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
  | 'take'

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

  // Declared in here, not at module scope: this body is stringified into the
  // guest page, where a reference to anything outside it is a ReferenceError
  // reported only as "Script failed to execute".
  //
  // Longest edge of an attached image and of its thumbnail. A UI screenshot is
  // legible far below its native size and the model reads it just as well.
  const SHOT_MAX_EDGE = 1400
  const THUMB_MAX_EDGE = 96
  /** Enough for a before, an after and a reference. More in one comment is a
   *  sign it wanted to be two comments. */
  const MAX_SHOTS = 4

  const state = (holder[STATE_KEY] as Record<string, unknown> | undefined) ?? {
    armed: false,
    drag: null,
    hidden: false,
    pending: [],
    pins: [],
    seq: 0,
    shotData: {}
  }

  holder[STATE_KEY] = state

  // A seeded state predates these fields. Nothing else re-checks them, so this
  // is the one place they are guaranteed to exist.
  if (!state.shotData) {state.shotData = {}}

  if (!state.pending) {state.pending = []}

  const pins = state.pins as Record<string, unknown>[]
  const shotData = state.shotData as Record<string, string>
  const pending = state.pending as string[]

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
      // The bubble sizes itself to the page it is floating over: comfortable on
      // a desktop layout, still inside the margins on a 320px phone viewport.
      '.bubble{position:fixed;width:min(304px,calc(100vw - 24px));background:#1c1b19;',
      'color:#eae7e1;border:1px solid #35322e;border-radius:12px;padding:10px;',
      'pointer-events:auto;box-shadow:0 10px 34px rgba(0,0,0,.5),0 1px 0 rgba(255,255,255,.04) inset;',
      'font:13px/1.5 system-ui,sans-serif;box-sizing:border-box}',
      '.bubble *{box-sizing:border-box}',
      // Head: the pin's own number and what it is fastened to, so an open
      // bubble is never ambiguous about which marker it belongs to.
      '.head{display:flex;align-items:center;gap:6px;margin:0 0 8px;color:#a09a91;font-size:11px}',
      '.head b{display:inline-flex;align-items:center;justify-content:center;min-width:17px;',
      'height:17px;padding:0 4px;border-radius:9px;background:#d99a5b;color:#1c1b19;font:700 10px system-ui}',
      '.head span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}',
      '.bubble textarea{display:block;width:100%;min-height:56px;max-height:168px;',
      'background:#131211;color:#f0ede8;border:1px solid #35322e;border-radius:8px;',
      // No resize grip: it drew a corner wart and the field grows on its own.
      'padding:8px;font:13px/1.5 system-ui,sans-serif;resize:none;outline:none;',
      // The guest page's own scrollbar is whatever Chromium gives that site —
      // on a plain page that is the chunky legacy bar WITH stepper arrows, and
      // it lands inside a 304px bubble looking like a piece of another app.
      // Styled here rather than left alone: the field is ours, so its scrollbar
      // has to be too. Deliberately NOT the standard `scrollbar-width`/
      // `scrollbar-color`: setting either makes Chromium ignore every
      // ::-webkit-scrollbar rule below and draw its own thin bar instead — the
      // gutter measured 10px with both present, and the thumb colour and the
      // dropped stepper arrows went with it. The preview guest is always
      // Chromium, so the pseudo-elements alone are the honest mechanism.
      'padding-inline-end:2px}',
      '.bubble textarea::placeholder{color:#6f6a63}',
      // Half the gutter is transparent border, so the thumb reads as a 4px
      // hairline with breathing room instead of a bar wedged against the text.
      '.bubble textarea::-webkit-scrollbar{width:8px;height:8px}',
      '.bubble textarea::-webkit-scrollbar-track{background:transparent}',
      '.bubble textarea::-webkit-scrollbar-thumb{background:#3b3833;border-radius:8px;',
      'border:2px solid transparent;background-clip:content-box}',
      '.bubble textarea:hover::-webkit-scrollbar-thumb{background:#565149;background-clip:content-box}',
      // The stepper arrows are the loudest part of the legacy bar and nobody
      // clicks them in a 168px-tall field.
      '.bubble textarea::-webkit-scrollbar-button{display:none;width:0;height:0}',
      '.bubble textarea::-webkit-scrollbar-corner{background:transparent}',
      '.bubble textarea:focus{border-color:#d99a5b;box-shadow:0 0 0 3px rgba(217,154,91,.16)}',
      '.row{display:flex;align-items:center;gap:6px;margin-top:9px}',
      '.bubble button{border:1px solid #35322e;border-radius:8px;background:#26241f;',
      'color:#eae7e1;font:600 12px system-ui,sans-serif;cursor:pointer;padding:6px 12px;',
      'display:inline-flex;align-items:center;justify-content:center;gap:5px;transition:background .12s}',
      '.bubble button:hover{background:#312e28}',
      '.bubble button:focus-visible{outline:2px solid #d99a5b;outline-offset:1px}',
      // Icon-only actions stay quiet; the one committing action is the loud one.
      '.bubble button.icon{padding:6px;width:30px;height:30px;background:transparent;border-color:transparent;color:#a09a91}',
      '.bubble button.icon:hover{background:#26241f;color:#eae7e1}',
      '.bubble button.icon.danger:hover{background:#3a2020;color:#ef9a9a}',
      '.bubble button.go{margin-inline-start:auto;background:#d99a5b;color:#1c1b19;border-color:#d99a5b}',
      '.bubble button.go:hover{background:#e4a869}',
      '.bubble svg{width:15px;height:15px;fill:none;stroke:currentColor;stroke-width:1.7;',
      'stroke-linecap:round;stroke-linejoin:round}',
      '.strip{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}',
      '.strip figure{position:relative;margin:0;width:52px;height:40px;border-radius:7px;',
      'overflow:hidden;border:1px solid #35322e;background:#0e0d0c}',
      '.strip img{width:100%;height:100%;object-fit:cover;display:block}',
      '.strip span{position:absolute;top:2px;inset-inline-end:2px;width:16px;height:16px;',
      'line-height:15px;text-align:center;border-radius:50%;background:rgba(12,11,10,.82);',
      'color:#fff;font:700 11px system-ui;cursor:pointer;opacity:0;transition:opacity .12s}',
      '.strip figure:hover span{opacity:1}',
      '.hint{margin-top:8px;color:#7d776f;font:11px/1.4 system-ui,sans-serif}',
      '.bubble.over{border-color:#d99a5b;box-shadow:0 0 0 3px rgba(217,154,91,.2)}',
      // A marker whose comment carries an image says so, so the strip is not a
      // surprise waiting inside a bubble nobody reopens.
      '.pin.shot::after{content:"";position:absolute;right:-2px;bottom:-2px;width:7px;',
      'height:7px;border-radius:50%;background:#eae7e1;box-shadow:0 0 0 1.5px #1c1b19}'
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
      const shots = (pin.shots as unknown[] | undefined) ?? []
      marker.className =
        'pin' +
        (pin.resolved ? ' resolved' : '') +
        (pin.orphaned ? ' orphan' : '') +
        (shots.length ? ' shot' : '')
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

  /**
   * Shrink an image and hand back a data URL.
   *
   * Two passes per attachment: one bounded copy that goes to the model, one
   * thumbnail small enough to ride in every state report and every navigation
   * seed without being felt. A pasted retina screenshot is several megabytes
   * and none of them buy the model anything.
   */
  const shrink = (
    source: string,
    edge: number,
    quality: number,
    done: (data: null | string, w: number, h: number) => void
  ) => {
    const image = new Image()

    image.onload = () => {
      const scale = Math.min(1, edge / Math.max(1, Math.max(image.width, image.height)))
      const w = Math.max(1, Math.round(image.width * scale))
      const h = Math.max(1, Math.round(image.height * scale))
      const canvas = doc.createElement('canvas')
      canvas.width = w
      canvas.height = h
      const ctx = canvas.getContext('2d')

      // No 2D context means no canvas at all (jsdom, a locked-down page). The
      // original is worse but correct; refusing the paste would be worse still.
      if (!ctx) {
        done(source, image.width || w, image.height || h)

        return
      }

      ctx.drawImage(image, 0, 0, w, h)

      try {
        done(canvas.toDataURL('image/jpeg', quality), w, h)
      } catch {
        done(source, w, h)
      }
    }

    image.onerror = () => done(null, 0, 0)
    image.src = source
  }

  /**
   * Where to open a comment, given where the user clicked and what they clicked.
   *
   * `avoid` is the pinned element's box. Opening on top of the thing being
   * commented on is the one placement that is always wrong — you cannot look at
   * the problem while you describe it — so the bubble goes under it when there
   * is room and above it when there is not, falling back to the cursor for
   * region pins, which have no element to dodge.
   */
  const openBubble = (
    pinId: string,
    left: number,
    top: number,
    avoid?: { bottom: number; left: number; top: number } | null
  ) => {
    closeBubble()
    const pin = pins.find(entry => entry.id === pinId)

    if (!pin) {return}
    const view = doc.defaultView
    const bubble = doc.createElement('div')
    bubble.className = 'bubble'

    /**
     * Put the bubble where it fits.
     *
     * Measured after every change rather than clamped against a guessed size:
     * the bubble grows when an image is added, and a hardcoded height puts it
     * half off the bottom of the window the moment it does.
     */
    const place = () => {
      const box = bubble.getBoundingClientRect()
      const vw = view ? view.innerWidth : 800
      const vh = view ? view.innerHeight : 600
      const width = box.width || 250
      const height = box.height || 140
      let x = left
      let y = top

      if (avoid) {
        x = avoid.left
        y = avoid.bottom + 10

        // No room underneath: sit above it instead of hanging off the fold.
        if (y + height + 8 > vh) {y = avoid.top - height - 10}
      }

      bubble.style.left = Math.max(8, Math.min(x, vw - width - 8)) + 'px'
      bubble.style.top = Math.max(8, Math.min(y, vh - height - 8)) + 'px'
    }

    /** A stroke icon, drawn rather than imported — the guest page has no icon
     *  font and no way to reach the app's. */
    const icon = (path: string) => {
      const svg = doc.createElementNS('http://www.w3.org/2000/svg', 'svg')
      svg.setAttribute('viewBox', '0 0 24 24')
      const shape = doc.createElementNS('http://www.w3.org/2000/svg', 'path')
      shape.setAttribute('d', path)
      svg.append(shape)

      return svg
    }

    const head = doc.createElement('div')
    head.className = 'head'
    const badge = doc.createElement('b')
    badge.textContent = String(pins.indexOf(pin) + 1)
    const where = doc.createElement('span')
    where.textContent = String(pin.target || 'region')
    head.append(badge, where)

    const area = doc.createElement('textarea')
    area.value = String(pin.comment || '')
    area.placeholder = 'What should change here?'
    // The comment is the user's own language, and this app is used in Arabic.
    // `auto` aligns and places the caret per what they actually type instead of
    // forcing every comment to read left-to-right.
    area.setAttribute('dir', 'auto')

    /** Grow with the text, up to the CSS cap, then scroll. */
    const grow = () => {
      area.style.height = 'auto'
      area.style.height = Math.min(168, Math.max(56, area.scrollHeight)) + 'px'
    }

    const strip = doc.createElement('div')
    strip.className = 'strip'

    const hint = doc.createElement('div')
    hint.className = 'hint'
    // One line. Two lines of instructions under a two-line comment is more
    // chrome than content.
    hint.textContent = 'Paste or drop an image · Esc to close'

    const shotsOf = () => (pin.shots as Record<string, unknown>[] | undefined) ?? []

    const drawStrip = () => {
      strip.textContent = ''

      for (const shot of shotsOf()) {
        const figure = doc.createElement('figure')
        const thumb = doc.createElement('img')
        thumb.src = String(shot.thumb || '')
        const drop = doc.createElement('span')
        drop.textContent = '×'
        drop.title = 'Remove image'
        drop.addEventListener('click', event => {
          event.stopPropagation()
          const list = shotsOf().filter(entry => entry.id !== shot.id)
          pin.shots = list
          delete shotData[String(shot.id)]
          drawStrip()
          paint()
        })
        figure.append(thumb, drop)
        strip.append(figure)
      }

      place()
    }

    /** Take a File list from a paste, a drop or the picker. */
    const ingest = (files: ArrayLike<File> | null) => {
      if (!files) {return}

      for (let index = 0; index < files.length; index += 1) {
        const file = files[index]

        if (!file || !String(file.type || '').startsWith('image/')) {continue}

        if (shotsOf().length >= MAX_SHOTS) {
          hint.textContent = 'Up to ' + MAX_SHOTS + ' images per comment'

          break
        }

        const reader = new FileReader()

        reader.onload = () => {
          const source = String(reader.result || '')

          if (!source) {return}
          shrink(source, SHOT_MAX_EDGE, 0.85, (full, w, h) => {
            if (!full) {return}
            shrink(full, THUMB_MAX_EDGE, 0.5, thumb => {
              const id = 'shot-' + now().toString(36) + '-' + Math.round(Math.random() * 1e6).toString(36)
              // The bytes stay here only until the app drains them; the pin
              // itself never carries more than the thumbnail.
              shotData[id] = full
              pending.push(id)
              pin.shots = shotsOf().concat([{ h, id, thumb: thumb || full, w }])
              drawStrip()
              paint()
            })
          })
        }

        reader.readAsDataURL(file)
      }
    }

    const picker = doc.createElement('input')
    picker.type = 'file'
    picker.accept = 'image/*'
    picker.multiple = true
    picker.style.display = 'none'
    picker.addEventListener('change', () => {
      ingest(picker.files)
      picker.value = ''
    })

    const row = doc.createElement('div')
    row.className = 'row'
    const add = doc.createElement('button')
    add.className = 'icon'
    add.title = 'Attach an image'
    add.append(icon('M3 5h18v14H3zM3 16l5-5 4 4 3-3 6 6'))
    const remove = doc.createElement('button')
    remove.className = 'icon danger'
    remove.title = 'Delete this comment'
    remove.append(icon('M4 7h16M9 7V5h6v2M6 7l1 13h10l1-13'))
    const save = doc.createElement('button')
    save.className = 'go'
    save.textContent = 'Done'

    add.addEventListener('click', event => {
      event.stopPropagation()
      picker.click()
    })
    save.addEventListener('click', event => {
      event.stopPropagation()
      pin.comment = area.value
      closeBubble()
      paint()
    })
    remove.addEventListener('click', event => {
      event.stopPropagation()
      const index = pins.findIndex(entry => entry.id === pinId)

      if (index !== -1) {
        for (const shot of shotsOf()) {delete shotData[String(shot.id)]}
        pins.splice(index, 1)
      }

      closeBubble()
      paint()
    })

    // Keep the comment as it is typed. Losing a paragraph to an Escape pressed
    // out of habit is not a trade worth making for a tidier save path.
    area.addEventListener('input', () => {
      pin.comment = area.value
      grow()
      place()
    })

    // Typing in the page must not reach the page. A review comment containing
    // "d" should not trigger the app's own keyboard shortcut for it.
    for (const type of ['keydown', 'keyup', 'keypress', 'paste']) {
      area.addEventListener(type, event => event.stopPropagation())
    }

    area.addEventListener('keydown', event => {
      if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
        event.preventDefault()
        pin.comment = area.value
        closeBubble()
        paint()
      }
    })

    area.addEventListener('paste', event => {
      const data = (event as ClipboardEvent).clipboardData
      const files = data ? data.files : null

      if (!files || !files.length) {return}
      // Otherwise the filename lands in the textarea as text next to the image.
      event.preventDefault()
      ingest(files)
    })

    bubble.addEventListener('dragover', event => {
      event.preventDefault()
      event.stopPropagation()
      bubble.classList.add('over')
    })
    bubble.addEventListener('dragleave', () => bubble.classList.remove('over'))
    bubble.addEventListener('drop', event => {
      event.preventDefault()
      event.stopPropagation()
      bubble.classList.remove('over')
      const data = (event as DragEvent).dataTransfer
      ingest(data ? data.files : null)
    })

    // Quiet actions first, the committing one pushed to the end by CSS: the
    // eye lands on Done, and Delete is never a neighbour of it.
    row.append(add, remove, save)
    bubble.append(head, area, strip, hint, row, picker)
    shadow().append(bubble)
    grow()
    drawStrip()
    area.focus()
    // Put the caret after the existing text rather than selecting all of it,
    // so reopening a comment to add a sentence does not risk wiping it.
    area.setSelectionRange(area.value.length, area.value.length)
  }

  // ---- annotation mode ---------------------------------------------------

  /**
   * Is this gesture ours?
   *
   * Everything the overlay draws lives in the shadow root, so a click on the
   * comment bubble's Save button arrives at the document listeners first. The
   * swallowers below must let it through or the bubble's own controls stop
   * working the moment annotation mode is on.
   */
  const insideOverlay = (event: Event) => {
    const node = doc.getElementById(HOST_ID)

    if (!node) {return false}
    const path = typeof event.composedPath === 'function' ? event.composedPath() : []

    for (const step of path) {if (step === node) {return true}}

    return false
  }

  const targetAt = (x: number, y: number): Element | null => {
    // The host is pointer-events:none at the root, but a marker inside it is
    // not, so ask the page what is under the cursor with the host discounted.
    const found = doc.elementFromPoint(x, y)

    if (!found || found.id === HOST_ID) {return null}

    return found
  }

  /**
   * Stop the page acting on a gesture that was meant for us.
   *
   * `preventDefault` on mouseup does NOT cancel the click the browser
   * synthesises afterwards, so commenting on a link still followed it. This is
   * the listener that actually holds the page still; the mouseup one only
   * suppresses the default action of the press itself.
   */
  const onClick = (event: MouseEvent) => {
    if (!state.armed || insideOverlay(event)) {return}
    event.preventDefault()
    event.stopPropagation()
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
    if (!state.armed || insideOverlay(event)) {return}
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
    if (!state.armed || insideOverlay(event)) {return}
    let placedOver: { bottom: number; left: number; top: number } | null = null
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
      const box = el.getBoundingClientRect()
      placedOver = { bottom: box.bottom, left: box.left, top: box.top }
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
    openBubble(id, event.clientX + 14, event.clientY + 14, placedOver)
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
    // Reopening: find the element again so the comment dodges it just as it did
    // when it was placed.
    const pin = pins.find(entry => entry.id === id)
    const match = pin && pin.kind === 'element' && pin.anchor ? kit.resolve(pin.anchor as never) : null
    const box = match?.element ? match.element.getBoundingClientRect() : null
    openBubble(
      id,
      event.clientX + 14,
      event.clientY + 14,
      box ? { bottom: box.bottom, left: box.left, top: box.top } : null
    )
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
    doc.addEventListener('click', onClick as EventListener, true)
    doc.addEventListener('keydown', onKey, true)
    const view = doc.defaultView

    if (view) {
      view.addEventListener('scroll', onScroll, true)
      view.addEventListener('resize', onScroll, true)
    }

    state.handlers = { onClick, onDown, onKey, onMove, onScroll, onUp }
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
      doc.removeEventListener('click', handlers.onClick, true)
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

  /** Full image bytes, for the one verb that asks for them. */
  let taken: null | string = null

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

      if (index !== -1) {
        for (const shot of (pins[index].shots as Record<string, unknown>[] | undefined) ?? []) {
          delete shotData[String(shot.id)]
        }

        pins.splice(index, 1)
      }

      paint()

      break
    }

    case 'clear':
      pins.splice(0, pins.length)

      for (const key of Object.keys(shotData)) {delete shotData[key]}
      pending.splice(0, pending.length)
      closeBubble()
      paint()

      break
    /**
     * Hand one image's bytes to the app and forget them here.
     *
     * The page is a bad place to keep megabytes: a navigation drops them, and
     * anything still here rides along in the next state report. The app takes
     * them the moment it hears about them and becomes the only owner.
     */
    case 'take': {
      const id = String(command.id ?? '')
      taken = shotData[id] ?? null
      delete shotData[id]
      const slot = pending.indexOf(id)

      if (slot !== -1) {pending.splice(slot, 1)}

      break
    }

    case 'state':

    default:
      break
  }

  return {
    armed: state.armed === true,
    hidden: state.hidden === true,
    // Announced on EVERY report, not just while annotating. An image pasted
    // and then left alone — Escape, or the panel closed — still has to reach
    // the app before the next navigation drops the page holding it.
    pendingShots: pending.slice(),
    pins: JSON.parse(JSON.stringify(pins)),
    shot: taken,
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
