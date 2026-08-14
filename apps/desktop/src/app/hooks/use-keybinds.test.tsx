import { fireEvent, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'

import { onComposerDictateRequest } from '@/app/chat/composer/focus'
import { $dictateMode, resetBinding, setBinding } from '@/store/keybinds'

import { useKeybinds } from './use-keybinds'

const deps = {
  openNewSessionTab: () => undefined,
  startFreshSession: () => undefined,
  toggleCommandCenter: () => undefined,
  toggleSelectedPin: () => undefined
}

const flushDispatch = () => new Promise(resolve => window.setTimeout(resolve, 0))

afterEach(() => {
  resetBinding('composer.dictate')
  $dictateMode.set('hold')
})

describe('composer.dictate keybind', () => {
  it('starts once on keydown and stops on its matching keyup, ignoring repeat', async () => {
    setBinding('composer.dictate', ['alt+v'])
    const requests: string[] = []
    const off = onComposerDictateRequest(({ request }) => requests.push(request))

    renderHook(() => useKeybinds(deps), { wrapper: MemoryRouter })

    fireEvent.keyDown(window, { altKey: true, code: 'KeyV', key: 'v' })
    fireEvent.keyDown(window, { altKey: true, code: 'KeyV', key: 'v', repeat: true })
    fireEvent.keyUp(window, { altKey: true, code: 'KeyV', key: 'v' })
    await flushDispatch()
    off()

    expect(requests).toEqual(['start', 'stop'])
  })
})
