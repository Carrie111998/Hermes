# cosmic-toplevel-list

A one-shot COSMIC (`cosmic-comp`) toplevel enumerator used by Hermes Desktop's
HUD to provide window-under awareness on the native Wayland COSMIC session.

It connects to the compositor over the Wayland protocol
`ext_foreign_toplevel_list_v1` and prints every open toplevel as JSON:

```json
[
  {
    "title": "brdpest@pop-os: ~ — COSMIC Terminal",
    "app_id": "com.system76.CosmicTerm",
    "identifier": "fMAyKoCdzve7Y9USulejTNjBVS8izFyS",
    "geometry": null
  }
]
```

`--active-only` prints just the focused window.

## Build

```sh
cargo build --release
# binary: target/release/cosmic-toplevel-list
```

Place the binary on `PATH` (or next to the Hermes Desktop executable) when
packaging. `apps/desktop/electron/cosmic.ts` shells out to it on COSMIC; if it
is missing, Hermes falls back to the X11 enumerator (which works under
XWayland, see `desktop.ozone_platform_hint`).

## Why a separate binary?

The Wayland client is written in Rust (`wayland-client` +
`cosmic-protocols`). Shipping it as a small prebuilt helper keeps the Electron
app's Node dependency surface unchanged and mirrors how the app already shells
out to platform tools (`xprop`, Hyprland's socket).

## Known COSMIC 1.0 limitation

`cosmic-comp` 1.0 serves `title`/`app_id`/`identifier` but does **not** emit
`geometry` or `pid` over its `zcosmic_toplevel_info_v1` extension. Geometry is
therefore reported as `null`; for pixel-exact window positions, run Hermes
under XWayland (`desktop.ozone_platform_hint: x11`).
