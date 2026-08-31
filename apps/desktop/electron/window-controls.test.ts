import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  DEFAULT_WINDOW_CONTROLS_MODE,
  isCompositorManagedWaylandSession,
  nativeWindowControlsEnabled,
  normalizeWindowControlsMode
} from './window-controls'

test('invalid window-controls values use the system default', () => {
  assert.equal(normalizeWindowControlsMode(undefined), DEFAULT_WINDOW_CONTROLS_MODE)
  assert.equal(normalizeWindowControlsMode('anything'), DEFAULT_WINDOW_CONTROLS_MODE)
})

test('system mode hides controls on compositor-managed Wayland and shows them elsewhere', () => {
  assert.equal(nativeWindowControlsEnabled('system', true), false)
  assert.equal(nativeWindowControlsEnabled('system', false), true)
})

test('explicit window-controls modes override compositor detection', () => {
  assert.equal(nativeWindowControlsEnabled('native', true), true)
  assert.equal(nativeWindowControlsEnabled('hidden', false), false)
})

test('known tiling Wayland compositors are compositor-managed sessions', () => {
  for (const desktop of ['Hyprland', 'Sway', 'river', 'niri', 'dwl']) {
    assert.equal(
      isCompositorManagedWaylandSession({ XDG_CURRENT_DESKTOP: desktop, XDG_SESSION_TYPE: 'wayland' }),
      true,
      desktop
    )
  }
})

test('Hyprland instance signatures identify the session without desktop variables', () => {
  assert.equal(isCompositorManagedWaylandSession({ HYPRLAND_INSTANCE_SIGNATURE: 'instance' }), true)
})

test('GNOME and KDE remain native-control defaults', () => {
  assert.equal(isCompositorManagedWaylandSession({ XDG_CURRENT_DESKTOP: 'GNOME', XDG_SESSION_TYPE: 'wayland' }), false)
  assert.equal(isCompositorManagedWaylandSession({ XDG_CURRENT_DESKTOP: 'KDE', XDG_SESSION_TYPE: 'wayland' }), false)
})
