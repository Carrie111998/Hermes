import { describe, expect, it } from 'vitest'

import {
  directActionGestureText,
  eligibleComposerText
} from '../../shared/direct-action-gesture'

function composer(eligible = true) {
  const root = document.createElement('form')
  const editor = document.createElement('div')
  const send = document.createElement('button')

  root.dataset.slot = 'composer-root'

  if (eligible) {
    root.dataset.directActionEligible = 'true'
  }

  editor.dataset.slot = 'composer-rich-input'
  editor.contentEditable = 'true'
  editor.innerHTML = 'log @apps/desktop/'
  send.dataset.directActionSend = 'true'
  root.append(editor, send)

  return { editor, root, send }
}

function trustedEvent(
  type: 'click' | 'keydown',
  target: HTMLElement,
  extras: Record<string, unknown> = {}
): Event {
  return {
    altKey: false,
    ctrlKey: false,
    isComposing: false,
    isTrusted: true,
    key: 'Enter',
    metaKey: false,
    shiftKey: false,
    target,
    type,
    ...extras
  } as unknown as Event
}

describe('Desktop direct-action gesture capture', () => {
  it('canonicalizes editor Enter before asynchronous submit work', () => {
    const { editor } = composer()

    expect(directActionGestureText(trustedEvent('keydown', editor))).toBe(
      'log @folder:`apps/desktop`'
    )
  })

  it('accepts the trusted click emitted by pointer or keyboard button activation', () => {
    const { send } = composer()

    expect(directActionGestureText(trustedEvent('click', send))).toBe(
      'log @folder:`apps/desktop`'
    )
  })

  it('rejects synthetic, modified, and ineligible sends', () => {
    const { editor, send } = composer(false)

    expect(eligibleComposerText(editor)).toBeNull()
    expect(directActionGestureText(trustedEvent('keydown', editor))).toBeNull()
    expect(
      directActionGestureText(
        trustedEvent('keydown', editor, { isTrusted: false })
      )
    ).toBeNull()
    expect(
      directActionGestureText(
        trustedEvent('keydown', editor, { shiftKey: true })
      )
    ).toBeNull()
    expect(directActionGestureText(trustedEvent('click', send))).toBeNull()
  })
})
