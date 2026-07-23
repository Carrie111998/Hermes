import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'
import fs from 'fs'
import { createRequire } from 'node:module'

// `hgui` symlinks a worktree's node_modules to the main checkout. Vite realpaths
// those before enforcing server.fs.allow, so codicon/font assets resolve outside
// the worktree root and 404. Whitelist the real node_modules locations.
const real = (p: string): string | null => {
  try {
    return fs.realpathSync(p)
  } catch {
    return null
  }
}

// Resolve packages through Node's own upward node_modules walk, anchored at this
// directory, instead of a hard-coded '../../node_modules'. The workspace root is
// two levels up ONLY in the main checkout: a git worktree (…/.claude/worktrees/
// <name>/apps/desktop) and an installed copy under %LOCALAPPDATA% both have no
// sibling node_modules there, and the hard-coded path made every renderer import
// of react fail with "Failed to resolve import \"react\"". Node's walk finds the
// hoisted copy from any depth, so the alias below keeps doing its real job —
// DEDUPING react to one instance — without pinning where that instance lives.
const requireFrom = createRequire(path.join(__dirname, 'package.json'))

// Package root (react → …/node_modules/react). 'react/package.json' is an
// explicit export of react 19, so this survives the exports map.
const pkgDir = (id: string): string | null => {
  try {
    return path.dirname(requireFrom.resolve(`${id}/package.json`))
  } catch {
    return null
  }
}

// Resolved entry FILE (react/jsx-runtime → …/node_modules/react/jsx-runtime.js).
const pkgEntry = (id: string): string | null => {
  try {
    return requireFrom.resolve(id)
  } catch {
    return null
  }
}

const reactDir = pkgDir('react')
const reactDomDir = pkgDir('react-dom')

// Order matters: @rollup/plugin-alias matches `id` and `id/*`, so the bare
// 'react' entry must stay ahead of the jsx-runtime ones exactly as before.
// Entries that fail to resolve are dropped rather than aliased to a bogus path,
// leaving Vite's default resolution in charge.
const reactAlias: Record<string, string> = Object.fromEntries(
  (
    [
      ['react', reactDir],
      ['react-dom', reactDomDir],
      ['react/jsx-dev-runtime', pkgEntry('react/jsx-dev-runtime')],
      ['react/jsx-runtime', pkgEntry('react/jsx-runtime')]
    ] as [string, string | null][]
  ).filter((entry): entry is [string, string] => entry[1] !== null)
)

const fsAllow = [
  ...new Set(
    [
      path.resolve(__dirname, '../..'),
      real(path.resolve(__dirname, 'node_modules')),
      real(path.resolve(__dirname, '../../node_modules')),
      // Wherever react ACTUALLY resolved from — outside the worktree root in a
      // worktree checkout, so the dev server would otherwise 403 its assets.
      ...[reactDir, reactDomDir].filter((d): d is string => d !== null).map(d => real(path.resolve(d, '..')))
    ].filter((p): p is string => p !== null)
  )
]

export default defineConfig({
  base: './',
  plugins: [react(), tailwindcss()],
  css: {
    // Pin an explicit (empty) PostCSS config. Tailwind is handled entirely by
    // `@tailwindcss/vite`, so the renderer needs no PostCSS plugins — and
    // without this, Vite's `postcss-load-config` walks UP the filesystem
    // looking for a stray `postcss.config.*` / `tailwind.config.*`. The desktop
    // build runs from inside the user's home tree (e.g.
    // `C:\Users\<name>\AppData\Local\hermes\hermes-agent\apps\desktop`), so an
    // unrelated Tailwind v3 config higher up the tree gets picked up and
    // reprocesses our v4 stylesheet, failing the build with
    // "`@layer base` is used but no matching `@tailwind base` directive is
    // present." Pinning the config makes the build hermetic.
    postcss: { plugins: [] }
  },
  build: {
    // Keep desktop packaging stable: Shiki ships many dynamic chunks by
    // default, and electron-builder can OOM scanning thousands of files.
    // Collapsing to a single chunk is intentional, so the renderer bundle is
    // large by design (~22 MB). Raise the warning ceiling above that so the
    // cosmetic "chunk larger than 500 kB" nag stays quiet, while still acting
    // as a regression alarm if the bundle balloons well past today's size.
    chunkSizeWarningLimit: 25000,
    rolldownOptions: {
      output: {
        codeSplitting: false
      }
    }
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
      '@hermes/plugin-sdk': path.resolve(__dirname, './src/sdk/index.ts'),
      '@hermes/shared/billing': path.resolve(__dirname, '../shared/src/billing-types.ts'),
      '@hermes/shared': path.resolve(__dirname, '../shared/src'),
      ...reactAlias
    },
    dedupe: ['react', 'react-dom']
  },
  server: {
    host: '127.0.0.1',
    port: 5174,
    strictPort: true,
    fs: {
      allow: fsAllow
    }
  },
  preview: {
    host: '127.0.0.1',
    port: 4174
  }
})
