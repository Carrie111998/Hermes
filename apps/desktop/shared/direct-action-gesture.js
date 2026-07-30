import { canonicalComposerSubmitText } from './composer-submit-text.js'

export const RICH_INPUT_SLOT = 'composer-rich-input'

export function composerPlainText(node) {
  if (node.nodeType === Node.TEXT_NODE) {
    return node.textContent || ''
  }
  if (node.nodeType !== Node.ELEMENT_NODE) {
    return ''
  }

  const element = node

  if (element.dataset.refText) {
    return element.dataset.refText
  }
  if (element.tagName === 'BR') {
    return '\n'
  }

  const text = Array.from(node.childNodes).map(composerPlainText).join('')
  const block = element.tagName === 'DIV' || element.tagName === 'P'

  return block && text && element.dataset.slot !== RICH_INPUT_SLOT
    ? `${text}\n`
    : text
}

export function eligibleComposerText(editor) {
  if (!editor || editor.dataset.slot !== RICH_INPUT_SLOT) {
    return null
  }

  const root = editor.closest('[data-slot="composer-root"]')

  if (root?.dataset.directActionEligible !== 'true') {
    return null
  }

  const text = canonicalComposerSubmitText(composerPlainText(editor))

  return text && text.length <= 8_192 ? text : null
}

export function directActionGestureText(event) {
  if (!event.isTrusted) {
    return null
  }

  if (event.type === 'keydown') {
    if (
      event.key !== 'Enter' ||
      event.shiftKey ||
      event.ctrlKey ||
      event.metaKey ||
      event.altKey ||
      event.isComposing
    ) {
      return null
    }

    const editor =
      event.target?.closest?.(`[data-slot="${RICH_INPUT_SLOT}"]`) || null

    return eligibleComposerText(editor)
  }

  if (event.type === 'click') {
    const send = event.target?.closest?.('[data-direct-action-send="true"]')
    const root = send?.closest?.('[data-slot="composer-root"]')
    const editor = root?.querySelector?.(
      `[data-slot="${RICH_INPUT_SLOT}"]`
    )

    return eligibleComposerText(editor)
  }

  return null
}

export function installDirectActionGestureCapture(target, begin) {
  const onKeyDown = event => {
    const text = directActionGestureText(event)

    if (text) {
      begin(text)
    }
  }

  const onClick = event => {
    const text = directActionGestureText(event)

    if (text) {
      begin(text)
    }
  }

  target.addEventListener('keydown', onKeyDown, true)
  target.addEventListener('click', onClick, true)

  return () => {
    target.removeEventListener('keydown', onKeyDown, true)
    target.removeEventListener('click', onClick, true)
  }
}
