// hud-focusable.ts — make the HUD's macOS NSPanel able to become the key window.
//
// An NSPanel never becomes key on its own, so `document.hasFocus()` stays
// false in the renderer no matter how many times `win.focus()` is called at
// creation — the composer never gets a caret and typing is swallowed. The pet
// overlay hits the same wall and works around it with the exact same call
// (see `hermes:pet-overlay:set-focusable` in main.ts); the HUD needs it
// unconditionally since, unlike the overlay's pop-up composer, it always
// wants the keyboard while open.

export function makeHudWindowFocusable(win: { setFocusable: (focusable: boolean) => void }): void {
  win.setFocusable(true)
}
