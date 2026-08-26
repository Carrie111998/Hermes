import { describe, expect, it } from 'vitest'

import { formatRefValue } from '@/components/assistant-ui/directive-text'
import {
  $composerMessageQuotes,
  clearComposerMessageQuotes,
  messageQuoteContextBlocks,
  reconcileComposerMessageQuotes,
  setComposerMessageQuote,
} from '@/store/composer'

describe('message quote context blocks', () => {
  it('resolves an @message:<id> ref to a quote code block', () => {
    setComposerMessageQuote('msg-abc', 'Hello from the assistant')

    const blocks = messageQuoteContextBlocks('Can you address @message:msg-abc?')

    expect(blocks).toEqual([
      '```quote<msg-abc>\nHello from the assistant\n```',
    ])

    clearComposerMessageQuotes()
  })

  it('returns empty array when draft has no @message refs', () => {
    setComposerMessageQuote('msg-xyz', 'Some text')

    expect(messageQuoteContextBlocks('Just a regular message')).toEqual([])

    clearComposerMessageQuotes()
  })

  it('returns empty array when the referenced id has no stored quote', () => {
    // Set a quote for a different id
    setComposerMessageQuote('msg-stored', 'Stored text')

    // Reference a different id that was never stored
    expect(messageQuoteContextBlocks('Check @message:msg-missing')).toEqual([])

    clearComposerMessageQuotes()
  })

  it('handles multiple distinct @message refs in one draft', () => {
    setComposerMessageQuote('msg-1', 'First quote')
    setComposerMessageQuote('msg-2', 'Second quote')

    const blocks = messageQuoteContextBlocks('@message:msg-1 and @message:msg-2')

    expect(blocks).toEqual([
      '```quote<msg-1>\nFirst quote\n```',
      '```quote<msg-2>\nSecond quote\n```',
    ])

    clearComposerMessageQuotes()
  })

  it('deduplicates repeated refs to the same message id', () => {
    setComposerMessageQuote('msg-dup', 'Single text')

    const blocks = messageQuoteContextBlocks(
      '@message:msg-dup and again @message:msg-dup'
    )

    expect(blocks).toHaveLength(1)
    expect(blocks[0]).toBe('```quote<msg-dup>\nSingle text\n```')

    clearComposerMessageQuotes()
  })

  it('handles quoted ref values (backtick-wrapped ids that need quoting)', () => {
    // IDs with special characters get wrapped by formatRefValue, and
    // messageIdsFromDraft strips the wrapping on the way back out.
    const id = 'msg:chaos-42'
    const formatted = formatRefValue(id)
    setComposerMessageQuote(id, 'Quoted id text')

    const blocks = messageQuoteContextBlocks(`See @message:${formatted}`)

    expect(blocks).toEqual([
      '```quote<msg:chaos-42>\nQuoted id text\n```',
    ])

    clearComposerMessageQuotes()
  })

  it('clearComposerMessageQuotes empties the store', () => {
    setComposerMessageQuote('msg-clear', 'Will be removed')

    expect(messageQuoteContextBlocks('@message:msg-clear')).toHaveLength(1)

    clearComposerMessageQuotes()

    expect(messageQuoteContextBlocks('@message:msg-clear')).toEqual([])
  })

  it('reconcileComposerMessageQuotes drops entries not present in the draft', () => {
    setComposerMessageQuote('msg-keep', 'Kept text')
    setComposerMessageQuote('msg-drop', 'Dropped text')

    reconcileComposerMessageQuotes('@message:msg-keep')

    // msg-drop should be gone after reconcile
    expect($composerMessageQuotes.get()['msg-drop']).toBeUndefined()
    expect($composerMessageQuotes.get()['msg-keep']).toBe('Kept text')

    clearComposerMessageQuotes()
  })

  it('reconcileComposerMessageQuotes preserves entries still referenced in the draft', () => {
    setComposerMessageQuote('msg-1', 'First')
    setComposerMessageQuote('msg-2', 'Second')

    reconcileComposerMessageQuotes('@message:msg-1 and @message:msg-2')

    expect($composerMessageQuotes.get()['msg-1']).toBe('First')
    expect($composerMessageQuotes.get()['msg-2']).toBe('Second')

    clearComposerMessageQuotes()
  })

  it('produces empty output for an empty draft', () => {
    setComposerMessageQuote('msg-1', 'Text')

    expect(messageQuoteContextBlocks('')).toEqual([])
    expect(messageQuoteContextBlocks('   ')).toEqual([])

    clearComposerMessageQuotes()
  })
})
