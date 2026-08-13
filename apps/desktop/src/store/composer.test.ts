import { afterEach, describe, expect, it } from 'vitest'

import { addComposerTextAttachment, type ComposerAttachment, mainComposerScope } from '@/store/composer'

describe('ComposerAttachment text kind', () => {
  afterEach(() => {
    mainComposerScope.clear()
  })

  it('addComposerTextAttachment stages a text chip with truncated preview', () => {
    addComposerTextAttachment('hello world', 'msg-1')
    const after = mainComposerScope.$attachments.get()

    expect(after.length).toBe(1)
    const chip = after[0]!
    expect(chip.kind).toBe('text')
    expect(chip.textContent).toBe('hello world')
    expect(chip.sourceMessageId).toBe('msg-1')
    expect(chip.label).toBe('hello world')
  })

  it('truncates preview label at 100 characters', () => {
    const longText = 'a'.repeat(200)
    addComposerTextAttachment(longText)
    const chip = mainComposerScope.$attachments.get()[0]!

    expect(chip.label.length).toBeLessThanOrEqual(101) // 100 chars + ellipsis
    expect(chip.label).toContain('\u2026')
    expect(chip.textContent).toBe(longText) // full text preserved
  })

  it('generates a unique id for each text attachment', () => {
    addComposerTextAttachment('first')
    addComposerTextAttachment('second')
    const after = mainComposerScope.$attachments.get()

    expect(after.length).toBe(2)
    expect(after[0]!.id).not.toBe(after[1]!.id)
  })

  it('accepts text kind in the ComposerAttachment type', () => {
    const attachment: ComposerAttachment = {
      id: 'text:test:1',
      kind: 'text',
      label: 'test',
      textContent: 'some text',
      sourceMessageId: 'msg-1',
    }
    expect(attachment.kind).toBe('text')
    expect(attachment.textContent).toBe('some text')
  })
})
