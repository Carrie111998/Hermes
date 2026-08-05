import { beforeEach, describe, expect, it, vi } from 'vitest'

import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { dialogTestApp, gridTestApp } from '../sdk/apps/index.js'
import { closeWidget, dispatchWidgetInput, launchWidget, openWidget } from '../sdk/host.js'
import { getWidgetApp, listWidgetApps } from '../sdk/registry.js'
import type { WidgetInput } from '../sdk/types.js'
import { widgetSdk } from '../sdk/userWidgets.js'

const key = (overrides: Partial<WidgetInput['key']> = {}, ch = ''): WidgetInput =>
  ({
    ch,
    key: { ctrl: false, escape: false, leftArrow: false, return: false, rightArrow: false, ...overrides }
  }) as WidgetInput

beforeEach(() => {
  resetOverlayState()
  resetUiState()
})

describe('widget SDK host', () => {
  it('exposes reactive UI and durable session identity to user widgets', async () => {
    const { defineWidgetApp } = await import('../sdk/registry.js')
    const { AmbientDock } = await import('../sdk/host.js')
    const { renderToScreen } = await import('../../packages/hermes-ink/src/ink/render-to-screen.js')
    const { cellAtIndex } = await import('../../packages/hermes-ink/src/ink/screen.js')
    const { createElement } = await import('react')

    const textOf = () => {
      const { screen, height } = renderToScreen(createElement(AmbientDock, { placement: 'dock-bottom' }), 100)

      return Array.from({ length: height * 100 }, (_, index) => cellAtIndex(screen, index).char).join('')
    }

    defineWidgetApp({
      help: 'identity test',
      id: 'identity-test',
      mode: 'ambient',
      init: () => ({}),
      reduce: state => state,
      render: () => {
        const identity = widgetSdk.useSessionIdentity()

        return widgetSdk.h(
          widgetSdk.Text,
          null,
          `${identity.uiSessionId}|${identity.durableSessionId}|${identity.profileId}|${identity.workspace}`
        )
      }
    })

    patchUiState({
      sid: 'ui-a',
      info: {
        cwd: '/work/a',
        model: 'm',
        profile_name: 'default',
        skills: {},
        stored_session_id: 'durable-a',
        tools: {}
      }
    })
    launchWidget('identity-test', '')
    expect(textOf().replace(/\s+/g, '')).toContain('ui-a|durable-a|default|/work/a')

    patchUiState({
      sid: 'ui-b',
      info: { cwd: '/work/b', model: 'm', profile_name: 'work', skills: {}, stored_session_id: 'durable-b', tools: {} }
    })
    expect(textOf().replace(/\s+/g, '')).toContain('ui-b|durable-b|work|/work/b')
  })

  it('isolates widget hooks across dynamic add and remove', async () => {
    const { PassThrough } = await import('stream')
    const { renderSync } = await import('@hermes/ink')
    const { createElement } = await import('react')
    const { AmbientDock } = await import('../sdk/host.js')
    const { defineWidgetApp } = await import('../sdk/registry.js')

    defineWidgetApp({
      help: 'hook order test',
      id: 'hook-order-test',
      mode: 'ambient',
      init: () => ({}),
      reduce: state => state,
      render: () => {
        const identity = widgetSdk.useSessionIdentity()

        return widgetSdk.h(widgetSdk.Text, null, identity.uiSessionId || 'no-session')
      }
    })

    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 })
    Object.assign(stdin, { isTTY: false })
    Object.assign(stderr, { isTTY: false })

    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const element = createElement(AmbientDock, { placement: 'dock-bottom' })

    const instance = renderSync(element, {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    try {
      expect(() => {
        launchWidget('hook-order-test', '')
        instance.rerender(element)
      }).not.toThrow()
      expect(() => {
        launchWidget('hook-order-test', '')
        instance.rerender(element)
      }).not.toThrow()
      expect(consoleError.mock.calls.flat().join(' ')).not.toMatch(/change in the order of Hooks|Rendered more hooks/)
    } finally {
      instance.unmount()
      instance.cleanup()
      consoleError.mockRestore()
    }
  })

  it('remounts modal renderers across app switches and same-id hot reloads', async () => {
    const { PassThrough } = await import('stream')
    const { renderSync } = await import('@hermes/ink')
    const { createElement, useRef } = await import('react')
    const { ActiveWidgetSlot } = await import('../sdk/host.js')
    const { defineWidgetApp } = await import('../sdk/registry.js')

    const seenRefs: string[] = []

    const ModalHookA = () => {
      const ref = useRef('a')
      seenRefs.push(`a:${ref.current}`)

      return widgetSdk.h(widgetSdk.Text, null, 'modal-a')
    }

    const ModalHookB = () => {
      const ref = useRef('b')
      seenRefs.push(`b:${ref.current}`)

      return widgetSdk.h(widgetSdk.Text, null, 'modal-b')
    }

    defineWidgetApp({
      help: 'modal hook a',
      id: 'modal-hook-a',
      mode: 'modal',
      init: () => ({}),
      reduce: state => state,
      render: ModalHookA
    })
    defineWidgetApp({
      help: 'modal hook b',
      id: 'modal-hook-b',
      mode: 'modal',
      init: () => ({}),
      reduce: state => state,
      render: ModalHookB
    })

    const stdout = new PassThrough()
    const stdin = new PassThrough()
    const stderr = new PassThrough()
    Object.assign(stdout, { columns: 100, isTTY: false, rows: 40 })
    Object.assign(stdin, { isTTY: false })
    Object.assign(stderr, { isTTY: false })

    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})
    const element = createElement(ActiveWidgetSlot)

    const instance = renderSync(element, {
      patchConsole: false,
      stderr: stderr as unknown as NodeJS.WriteStream,
      stdin: stdin as unknown as NodeJS.ReadStream,
      stdout: stdout as unknown as NodeJS.WriteStream
    })

    try {
      expect(() => {
        launchWidget('modal-hook-a', '')
        instance.rerender(element)
      }).not.toThrow()
      expect(() => {
        launchWidget('modal-hook-b', '')
        instance.rerender(element)
      }).not.toThrow()
      expect(seenRefs.at(-1)).toBe('b:b')

      const ModalHookBReloaded = () => {
        const ref = useRef('reloaded')
        seenRefs.push(`reload:${ref.current}`)

        return widgetSdk.h(widgetSdk.Text, null, 'modal-b-reloaded')
      }

      defineWidgetApp({
        help: 'modal hook b reloaded',
        id: 'modal-hook-b',
        mode: 'modal',
        init: () => ({}),
        reduce: state => state,
        render: ModalHookBReloaded
      })
      expect(() => instance.rerender(element)).not.toThrow()
      expect(seenRefs.at(-1)).toBe('reload:reloaded')
      expect(consoleError.mock.calls.flat().join(' ')).not.toMatch(
        /change in the order of Hooks|Rendered (fewer|more) hooks/
      )
    } finally {
      closeWidget()
      instance.unmount()
      instance.cleanup()
      consoleError.mockRestore()
    }
  })

  it('registers the reference apps', () => {
    expect(listWidgetApps().map(app => app.id)).toEqual(
      expect.arrayContaining(['dialog-test', 'grid-test', 'ticker', 'weather'])
    )
    expect(getWidgetApp('grid-test')).toBe(gridTestApp)
  })

  it('launch → dispatch → close lifecycle drives the overlay slot', () => {
    expect(launchWidget('grid-test', '5x2')).toBeNull()
    expect(getOverlayState().widget).toMatchObject({ appId: 'grid-test' })
    expect(getOverlayState().widget?.state).toMatchObject({ cols: 5, rows: 2 })

    // Reducer output lands back in the slot.
    expect(dispatchWidgetInput(key({}, 'l'))).toBe(true)
    expect(getOverlayState().widget?.state).toMatchObject({ activeCol: 1 })

    // null from reduce closes.
    expect(dispatchWidgetInput(key({ escape: true }))).toBe(true)
    expect(getOverlayState().widget).toBeNull()

    // Nothing active → not handled.
    expect(dispatchWidgetInput(key({}, 'x'))).toBe(false)
  })

  it('refused launches return the usage line and leave the slot empty', () => {
    expect(launchWidget('grid-test', 'not-a-size !')).toBe(gridTestApp.usage)
    expect(launchWidget('nope', '')).toMatch(/unknown widget app/)
    expect(getOverlayState().widget).toBeNull()
  })

  it('apps stack each other via the typed programmatic launch', () => {
    expect(launchWidget('grid-test', '')).toBeNull()

    // `d` swaps the active app to the dialog demo.
    expect(dispatchWidgetInput(key({}, 'd'))).toBe(true)
    expect(getOverlayState().widget).toMatchObject({ appId: 'dialog-test' })

    // Enter closes the dialog app.
    expect(dispatchWidgetInput(key({ return: true }))).toBe(true)
    expect(getOverlayState().widget).toBeNull()
  })

  it('a widget that throws in render shows an error chip, not a dead TUI', async () => {
    const { defineWidgetApp } = await import('../sdk/registry.js')
    const { AmbientDock } = await import('../sdk/host.js')
    const { renderToScreen } = await import('../../packages/hermes-ink/src/ink/render-to-screen.js')
    const { createElement } = await import('react')

    defineWidgetApp({
      help: 'crash test',
      id: 'crash-test',
      mode: 'ambient',
      init: () => ({}),
      reduce: state => state,
      render: () => {
        throw new Error('boom')
      }
    })

    launchWidget('crash-test', 'x')

    // Renders the boundary chip instead of propagating the throw.
    expect(() => renderToScreen(createElement(AmbientDock, { placement: 'dock-bottom' }), 60)).not.toThrow()
  })

  it('openWidget is a typed direct launch', () => {
    openWidget(dialogTestApp, { body: 'hi', zone: 'top-right' })
    expect(getOverlayState().widget).toMatchObject({ appId: 'dialog-test', state: { zone: 'top-right' } })
    closeWidget()
    expect(getOverlayState().widget).toBeNull()
  })

  it('a MODAL widget blocks the composer; ambient never does', async () => {
    const { $isBlocked } = await import('../app/overlayStore.js')

    expect($isBlocked.get()).toBe(false)
    launchWidget('ticker', '')
    expect($isBlocked.get()).toBe(false)
    launchWidget('dialog-test', 'center')
    expect($isBlocked.get()).toBe(true)
  })

  it('ambient zones route by the app contract (docks + floats)', async () => {
    const { defineWidgetApp } = await import('../sdk/registry.js')
    const { Text } = await import('@hermes/ink')
    const { createElement } = await import('react')

    defineWidgetApp({
      help: 'corner test app',
      id: 'corner-test',
      mode: 'ambient',
      zone: 'top-right',
      init: () => ({}),
      reduce: state => state,
      render: () => createElement(Text, null, 'corner')
    })

    launchWidget('corner-test', 'x')
    launchWidget('ticker', 'x')

    const zoneOf = (id: string) => getWidgetApp(id)?.zone ?? 'dock-bottom'

    expect(getOverlayState().ambient.map(a => [a.appId, zoneOf(a.appId)])).toEqual([
      ['corner-test', 'top-right'],
      ['ticker', 'dock-bottom']
    ])
  })

  it('rails reserve the widest railed app; docks reserve nothing sideways', async () => {
    const { ambientRailWidth } = await import('../sdk/host.js')
    const { defineWidgetApp } = await import('../sdk/registry.js')
    const { Text } = await import('@hermes/ink')
    const { createElement } = await import('react')

    defineWidgetApp({
      help: 'wide rail app',
      id: 'rail-wide',
      mode: 'ambient',
      width: 52,
      zone: 'top-right',
      init: () => ({}),
      reduce: state => state,
      render: () => createElement(Text, null, 'wide')
    })

    expect(ambientRailWidth('right')).toBe(0)
    launchWidget('corner-test', 'x') // top-right, default width 44
    launchWidget('rail-wide', 'x')
    launchWidget('ticker', 'x') // dock-bottom — no rail contribution

    expect(ambientRailWidth('right')).toBe(52)
    expect(ambientRailWidth('left')).toBe(0)
  })

  it('ambient apps dock together and toggle independently', () => {
    expect(launchWidget('ticker', 'eurusd')).toBeNull()
    expect(launchWidget('weather', '')).toBeNull()
    expect(getOverlayState().ambient.map(a => a.appId)).toEqual(['ticker', 'weather'])

    // Relaunch with no arg toggles just that app out of the dock.
    expect(launchWidget('ticker', '')).toBeNull()
    expect(getOverlayState().ambient.map(a => a.appId)).toEqual(['weather'])
  })
})
