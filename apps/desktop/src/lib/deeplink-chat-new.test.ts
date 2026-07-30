import { describe, expect, it, beforeEach } from 'vitest'

import {
  clearStickySessionId,
  cwdLooksSane,
  deeplinkStickyStorageKey,
  isChatNewDeliveryStale,
  isInstalledProfileName,
  nextChatNewDeliveryId,
  normalizeStickySlot,
  pushStickyDelivery,
  readStickySessionId,
  resetChatNewDeliverySeqForTests,
  setStickyPending,
  takeStickyDeliveryForSession,
  takeStickyPending,
  writeStickySessionId
} from './deeplink-chat-new'

function memoryStorage(): Storage {
  const map = new Map<string, string>()
  return {
    get length() {
      return map.size
    },
    clear() {
      map.clear()
    },
    getItem(key: string) {
      return map.has(key) ? map.get(key)! : null
    },
    key(index: number) {
      return [...map.keys()][index] ?? null
    },
    removeItem(key: string) {
      map.delete(key)
    },
    setItem(key: string, value: string) {
      map.set(key, String(value))
    }
  }
}

describe('cwdLooksSane', () => {
  it('accepts absolute unix paths', () => {
    expect(cwdLooksSane('/Users/trevor/HomeDome')).toBe(true)
    expect(cwdLooksSane('/tmp')).toBe(true)
  })

  it('accepts windows drive and unc paths', () => {
    expect(cwdLooksSane('C:\\Users\\trevor\\proj')).toBe(true)
    expect(cwdLooksSane('D:/work/repo')).toBe(true)
    expect(cwdLooksSane('\\\\server\\share\\repo')).toBe(true)
  })

  it('rejects relative and traversal', () => {
    expect(cwdLooksSane('relative/path')).toBe(false)
    expect(cwdLooksSane('../etc')).toBe(false)
    expect(cwdLooksSane('/Users/../etc')).toBe(false)
    expect(cwdLooksSane('/Users/trevor/..')).toBe(false)
    expect(cwdLooksSane('')).toBe(false)
  })
})

describe('sticky slots', () => {
  it('normalizes slot names', () => {
    expect(normalizeStickySlot(' CEO ')).toBe('ceo')
    expect(normalizeStickySlot('My Project!')).toBe('my-project')
    expect(normalizeStickySlot('')).toBe('')
  })

  it('round-trips sticky session ids', () => {
    const store = memoryStorage()
    writeStickySessionId('ceo', '20260724_session', store)
    expect(readStickySessionId('CEO', store)).toBe('20260724_session')
    expect(deeplinkStickyStorageKey('ceo')).toContain('ceo')
    clearStickySessionId('ceo', store)
    expect(readStickySessionId('ceo', store)).toBe(null)
  })
})

describe('delivery-correlated sticky pending', () => {
  beforeEach(() => {
    resetChatNewDeliverySeqForTests()
  })

  it('FIFO consume preserves order across rapid deliveries', () => {
    const store = memoryStorage()
    pushStickyDelivery({ deliveryId: '1', slot: 'ceo', profile: 'default' }, store)
    pushStickyDelivery({ deliveryId: '2', slot: 'work', profile: 'work' }, store)

    expect(takeStickyDeliveryForSession({ profile: 'default' }, store)).toBe('ceo')
    expect(takeStickyDeliveryForSession({ profile: 'work' }, store)).toBe('work')
    expect(takeStickyDeliveryForSession({ profile: 'default' }, store)).toBe(null)
  })

  it('profile match skips non-matching head without dropping it', () => {
    const store = memoryStorage()
    pushStickyDelivery({ deliveryId: '1', slot: 'ceo', profile: 'default' }, store)
    pushStickyDelivery({ deliveryId: '2', slot: 'lab', profile: 'work' }, store)

    expect(takeStickyDeliveryForSession({ profile: 'work' }, store)).toBe('lab')
    expect(takeStickyDeliveryForSession({ profile: 'default' }, store)).toBe('ceo')
  })

  it('null profile binding matches any session profile', () => {
    const store = memoryStorage()
    pushStickyDelivery({ deliveryId: '1', slot: 'ceo', profile: null }, store)
    expect(takeStickyDeliveryForSession({ profile: 'jordan' }, store)).toBe('ceo')
  })

  it('stale delivery id detects newer chat/new', () => {
    const a = nextChatNewDeliveryId()
    expect(isChatNewDeliveryStale(a)).toBe(false)
    const b = nextChatNewDeliveryId()
    expect(isChatNewDeliveryStale(a)).toBe(true)
    expect(isChatNewDeliveryStale(b)).toBe(false)
  })

  it('legacy setStickyPending still one-shot via takeStickyPending', () => {
    const store = memoryStorage()
    setStickyPending('work', store)
    expect(takeStickyPending(store)).toBe('work')
    expect(takeStickyPending(store)).toBe(null)
  })
})

describe('isInstalledProfileName', () => {
  it('allows empty profile', () => {
    expect(isInstalledProfileName('', [{ name: 'default' }])).toBe(true)
  })

  it('allow-lists against installed names', () => {
    const installed = [{ name: 'default', is_default: true }, { name: 'jordan' }, { name: 'work' }]
    expect(isInstalledProfileName('jordan', installed)).toBe(true)
    expect(isInstalledProfileName('Jordan', installed)).toBe(true)
    expect(isInstalledProfileName('evil', installed)).toBe(false)
  })

  it('boot empty list only allows default', () => {
    expect(isInstalledProfileName('default', [])).toBe(true)
    expect(isInstalledProfileName('jordan', [])).toBe(false)
  })
})
