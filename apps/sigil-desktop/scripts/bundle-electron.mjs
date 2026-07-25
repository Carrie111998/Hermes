import { build } from 'esbuild'

await build({
  bundle: true,
  entryPoints: ['electron/main.ts'],
  external: ['electron'],
  format: 'esm',
  outfile: 'dist/electron-main.mjs',
  platform: 'node',
  sourcemap: true
})

await build({
  bundle: true,
  entryPoints: ['electron/preload.ts'],
  external: ['electron'],
  format: 'cjs',
  outfile: 'dist/electron-preload.cjs',
  platform: 'node'
})
