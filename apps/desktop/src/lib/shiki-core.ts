import { useEffect, useState } from 'react'
import { createHighlighterCore, createOnigurumaEngine } from 'react-shiki/core'

export type CuratedHighlighter = Awaited<ReturnType<typeof createHighlighterCore>>

/**
 * Curated Shiki highlighter core.
 *
 * `react-shiki` (and its `shiki` dep) default to a FULL bundle that inlines
 * every one of Shiki's ~300 bundled grammars — 14.5 MB of the desktop
 * renderer's single chunk (67.8% of it), most of which the app never
 * highlights. Because the desktop build forces `codeSplitting: false`
 * (electron-builder OOMs scanning Shiki's default per-language chunks), that
 * whole grammar set lands eagerly in one file.
 *
 * Instead we build a core highlighter over a curated, statically-imported
 * language allowlist. The chosen set covers everything the app actually
 * renders: every value in `SHIKI_LANGUAGE_BY_EXTENSION` (the file-preview +
 * diff surface) plus the languages a coding agent realistically emits in chat
 * fences. A fence in a language we DON'T carry degrades to uncolored text
 * (react-shiki resolves an unloaded id to `plaintext`), never an error.
 *
 * Static `import(...)` of each grammar keeps the single-chunk shape: rolldown
 * inlines them into the one chunk (no per-language dynamic chunks), and drops
 * the ~260 grammars we omit — reclaiming ~8 MB.
 *
 * Keep this list a superset of `SHIKI_LANGUAGE_BY_EXTENSION` in
 * `markdown-code.ts`: a value there that is missing here would make source
 * previews + diffs of that file type lose highlighting.
 */

// Themes used across the app: chat/diff use github-dark-dimmed + light; the
// file preview uses github-dark-default + light. Load all three (themes are
// tiny next to grammars).
//
// These `import(...)` calls live inside factories (not module-scope consts) so
// the grammars only parse when the highlighter is first built — on the first
// code block — rather than eagerly at app startup.
const themeImports = () => [
  import('@shikijs/themes/github-dark-dimmed'),
  import('@shikijs/themes/github-dark-default'),
  import('@shikijs/themes/github-light-default')
]

const langImports = () => [
  // --- file-extension languages (must stay a superset of SHIKI_LANGUAGE_BY_EXTENSION) ---
  import('@shikijs/langs/astro'),
  import('@shikijs/langs/bash'),
  import('@shikijs/langs/c'),
  import('@shikijs/langs/cpp'),
  import('@shikijs/langs/clojure'),
  import('@shikijs/langs/csharp'),
  import('@shikijs/langs/css'),
  import('@shikijs/langs/dart'),
  import('@shikijs/langs/docker'),
  import('@shikijs/langs/elixir'),
  import('@shikijs/langs/fish'),
  import('@shikijs/langs/go'),
  import('@shikijs/langs/graphql'),
  import('@shikijs/langs/haskell'),
  import('@shikijs/langs/html'),
  import('@shikijs/langs/ini'),
  import('@shikijs/langs/java'),
  import('@shikijs/langs/julia'),
  import('@shikijs/langs/javascript'),
  import('@shikijs/langs/json'),
  import('@shikijs/langs/json5'),
  import('@shikijs/langs/jsonc'),
  import('@shikijs/langs/jsx'),
  import('@shikijs/langs/kotlin'),
  import('@shikijs/langs/less'),
  import('@shikijs/langs/lua'),
  import('@shikijs/langs/make'),
  import('@shikijs/langs/markdown'),
  import('@shikijs/langs/mdx'),
  import('@shikijs/langs/ocaml'),
  import('@shikijs/langs/nix'),
  import('@shikijs/langs/php'),
  import('@shikijs/langs/perl'),
  import('@shikijs/langs/proto'),
  import('@shikijs/langs/powershell'),
  import('@shikijs/langs/python'),
  import('@shikijs/langs/r'),
  import('@shikijs/langs/ruby'),
  import('@shikijs/langs/rust'),
  import('@shikijs/langs/sass'),
  import('@shikijs/langs/scala'),
  import('@shikijs/langs/scss'),
  import('@shikijs/langs/sql'),
  import('@shikijs/langs/svelte'),
  import('@shikijs/langs/swift'),
  import('@shikijs/langs/terraform'),
  import('@shikijs/langs/toml'),
  import('@shikijs/langs/typescript'),
  import('@shikijs/langs/tsx'),
  import('@shikijs/langs/vue'),
  import('@shikijs/langs/xml'),
  import('@shikijs/langs/yaml'),
  import('@shikijs/langs/zig'),
  // --- common chat-fence tail a coding agent realistically emits ---
  import('@shikijs/langs/diff'),
  import('@shikijs/langs/erlang'),
  import('@shikijs/langs/groovy'),
  import('@shikijs/langs/objective-c'),
  import('@shikijs/langs/cmake'),
  import('@shikijs/langs/hcl'),
  import('@shikijs/langs/batch'),
  import('@shikijs/langs/regexp'),
  import('@shikijs/langs/csv'),
  import('@shikijs/langs/http'),
  import('@shikijs/langs/properties'),
  import('@shikijs/langs/vim'),
  import('@shikijs/langs/xsl'),
  import('@shikijs/langs/wasm'),
  import('@shikijs/langs/asm')
]

// The ~260 grammars we deliberately DON'T load — a fence in one of these renders
// as plain (uncolored) text rather than erroring. See the module doc comment.

// Short fence tags → the canonical grammar id we loaded above. A custom core
// (unlike Shiki's full bundle) has no built-in alias map, and react-shiki
// resolves an unknown id to `plaintext` BEFORE the highlighter sees it — so we
// normalize the tag ourselves. Anything not listed passes through and, if not
// a loaded id, renders as plain text.
const LANG_ALIASES: Record<string, string> = {
  'c++': 'cpp',
  'c#': 'csharp',
  cs: 'csharp',
  dockerfile: 'docker',
  gql: 'graphql',
  golang: 'go',
  hs: 'haskell',
  jl: 'julia',
  js: 'javascript',
  kt: 'kotlin',
  md: 'markdown',
  markdown: 'markdown',
  mjs: 'javascript',
  cjs: 'javascript',
  objc: 'objective-c',
  patch: 'diff',
  perl: 'perl',
  pl: 'perl',
  ps: 'powershell',
  ps1: 'powershell',
  py: 'python',
  rb: 'ruby',
  rs: 'rust',
  sh: 'bash',
  shell: 'bash',
  shellscript: 'bash',
  tf: 'terraform',
  ts: 'typescript',
  yml: 'yaml',
  zsh: 'bash'
}

export function normalizeShikiLang(language: string | undefined | null): string {
  const tag = (language || '').trim().toLowerCase()

  if (!tag) {
    return 'text'
  }

  return LANG_ALIASES[tag] ?? tag
}

let highlighterPromise: null | Promise<CuratedHighlighter> = null

export function getCuratedHighlighter(): Promise<CuratedHighlighter> {
  if (!highlighterPromise) {
    highlighterPromise = createHighlighterCore({
      engine: createOnigurumaEngine(import('shiki/wasm')),
      langs: langImports(),
      themes: themeImports()
    })
  }

  return highlighterPromise
}

/**
 * Resolve the shared curated highlighter once. Returns `null` until it's ready;
 * callers render their plain-text fallback in the meantime (the same
 * plain→highlighted transition react-shiki's own async init produced before).
 */
export function useCuratedHighlighter(): CuratedHighlighter | null {
  const [highlighter, setHighlighter] = useState<CuratedHighlighter | null>(null)

  useEffect(() => {
    let active = true

    void getCuratedHighlighter().then(instance => {
      if (active) {
        setHighlighter(instance)
      }
    })

    return () => {
      active = false
    }
  }, [])

  return highlighter
}
