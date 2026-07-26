/**
 * brand-gates.ts — compile-time brand booleans for the Electron main process
 * and preload.
 *
 * DCE contract (why this is its own file): bundle-electron-main.mjs bakes
 * `process.env.HERMES_DESKTOP_BRAND` in via an esbuild define and bundles
 * with `minifySyntax: true`. esbuild's cross-module constant propagation then
 * inlines these booleans at every use site, so `if (IS_QUIZVERSE_BRAND)`
 * blocks in main.ts / preload.ts constant-fold away and the inactive brand's
 * modules (qv-deeptutor, the ix-* IPC surface, its manifest JSON, its preload
 * namespace) are dead-code-eliminated from the other brand's bundles.
 *
 * That propagation ONLY happens for constants declared in a module with NO
 * imports (esbuild bails on modules that import anything — verified
 * empirically; brand.ts imports the manifest JSONs, which is why the gates
 * live here and are re-exported there). Do NOT add imports to this file, and
 * do NOT route the env read through a variable or object lookup —
 * scripts/check-brand-separation.mjs scans dist/electron-main.mjs and
 * dist/electron-preload.js and fails the build if the other brand's markers
 * reappear.
 *
 * In dev (unbundled `electron .`) the plain env var applies at runtime with
 * the same default (ix-agency).
 *
 * Three brands: ix-agency (default), quizverse, foundrly. Agency is everything
 * that is neither quizverse nor foundrly so unset/dev defaults stay Agency.
 */
export const IS_QUIZVERSE_BRAND: boolean = process.env.HERMES_DESKTOP_BRAND === 'quizverse'
export const IS_FOUNDRLY_BRAND: boolean = process.env.HERMES_DESKTOP_BRAND === 'foundrly'
export const IS_IX_AGENCY_BRAND: boolean =
  process.env.HERMES_DESKTOP_BRAND !== 'quizverse' &&
  process.env.HERMES_DESKTOP_BRAND !== 'foundrly'
