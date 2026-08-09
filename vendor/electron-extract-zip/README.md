# Electron archive-extraction compatibility shim

This private package implements the `extract(zipPath, options)` API consumed by
Electron's installer by delegating to the pure-JavaScript `extract-zip` package.

Electron 41+ otherwise loads `@electron-internal/extract-zip`, which contains an
unsigned native Windows addon. Windows Smart App Control can block that addon
before Electron's installer can check an existing download or use an override;
see electron/electron#52481. The shim avoids that installation failure class
without downgrading Electron to a version with known security advisories.

The root manifest installs this package directly and uses an npm `$` override so
Electron receives the same dependency. Tests assert that the resolved package is
this repository path and contains no `.node` files.

## Removal

Remove this package, its root dependency/override, and the corresponding tests
once Electron ships an installer that either lazily loads the native extractor
with a JavaScript fallback or otherwise works under enforced Smart App Control.
