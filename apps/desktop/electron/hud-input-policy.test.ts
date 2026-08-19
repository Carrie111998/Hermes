/**
 * Unit tests for the HUD input policy. The point of these is the split: the
 * solid fallback has to reach the X11 windows where the input region never
 * comes back, and has to leave every other backend on the click-through path
 * it works fine on.
 */

import assert from 'node:assert/strict'

import { test } from 'vitest'

import { hudInputPolicy } from './hud-input-policy'

const X11_SESSION = { DISPLAY: ':0', XDG_SESSION_TYPE: 'x11' }
const WAYLAND_SESSION = { WAYLAND_DISPLAY: 'wayland-0', XDG_SESSION_TYPE: 'wayland' }
// A Wayland login with an X server behind it, which is what an XWayland client
// sees.
const XWAYLAND_SESSION = { DISPLAY: ':0', WAYLAND_DISPLAY: 'wayland-0', XDG_SESSION_TYPE: 'wayland' }

test('the click-through design is kept everywhere it works', () => {
  for (const platform of ['darwin', 'win32']) {
    assert.equal(hudInputPolicy(platform, {}, []), 'click-through')
    // Stray Linux session variables do not make a Mac an X11 box.
    assert.equal(hudInputPolicy(platform, X11_SESSION, []), 'click-through')
  }
})

test('an X11 session gets the solid HUD', () => {
  assert.equal(hudInputPolicy('linux', X11_SESSION, []), 'solid')
})

test('a Wayland session gets the solid HUD too, because Electron is on XWayland there', () => {
  // Nothing appends an --ozone-platform switch, so Electron takes its default
  // X11 backend and the window is an X11 window whatever the session says.
  // This is the KDE-Plasma-and-GNOME-alike case from the reports.
  assert.equal(hudInputPolicy('linux', WAYLAND_SESSION, []), 'solid')
  assert.equal(hudInputPolicy('linux', XWAYLAND_SESSION, []), 'solid')
})

test('asking for a native Wayland surface keeps the click-through path', () => {
  for (const argv of [['--ozone-platform=wayland'], ['--ozone-platform-hint=wayland']]) {
    assert.equal(hudInputPolicy('linux', WAYLAND_SESSION, argv), 'click-through')
  }

  assert.equal(
    hudInputPolicy('linux', { ...WAYLAND_SESSION, ELECTRON_OZONE_PLATFORM_HINT: 'wayland' }, []),
    'click-through'
  )
})

test('an auto hint follows the session', () => {
  assert.equal(hudInputPolicy('linux', WAYLAND_SESSION, ['--ozone-platform-hint=auto']), 'click-through')
  assert.equal(hudInputPolicy('linux', X11_SESSION, ['--ozone-platform-hint=auto']), 'solid')
  // Both displays present means an X server is there to fall back to, so auto
  // resolving to Wayland is the session's call, not ours.
  assert.equal(hudInputPolicy('linux', XWAYLAND_SESSION, ['--ozone-platform-hint=auto']), 'click-through')
})

test('asking for X11 on a Wayland session gets the solid HUD', () => {
  assert.equal(hudInputPolicy('linux', WAYLAND_SESSION, ['--ozone-platform=x11']), 'solid')
})

test('the explicit switch beats the hint, and the last switch wins', () => {
  assert.equal(
    hudInputPolicy('linux', WAYLAND_SESSION, ['--ozone-platform-hint=auto', '--ozone-platform=x11']),
    'solid'
  )
  assert.equal(
    hudInputPolicy('linux', X11_SESSION, ['--ozone-platform=x11', '--ozone-platform=wayland']),
    'click-through'
  )
})

test('a backend nobody recognises falls back to X11, the Linux default', () => {
  assert.equal(hudInputPolicy('linux', X11_SESSION, ['--ozone-platform=headless']), 'solid')
  assert.equal(hudInputPolicy('linux', {}, []), 'solid')
})
