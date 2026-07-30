const BRACKETED_PASTE_BOUNDARY_START = /(^|[\s\n>:\])])\[200~/g
const BRACKETED_PASTE_BOUNDARY_END = /\[201~(?=$|[\s\n<[():;.,!?])/g
const BRACKETED_PASTE_DEGRADED_START = /(^|[\s\n>:\])])00~/g
const BRACKETED_PASTE_DEGRADED_END = /01~(?=$|[\s\n<[():;.,!?])/g
const DESKTOP_PASTE_ARTIFACT = '~[[e'

export const REF_RE =
  /@(file|folder|url|image|tool|line|terminal|session):(`[^`\n]+`|"[^"\n]+"|'[^'\n]+'|\S+)/g

export const BARE_PATH_RE =
  /(?<![\w@/])@((?!(?:file|folder|url|image|tool|line|terminal|session|git):)[^\s@:]*\/[^\s@:]*)/g

export function stripLeakedBracketedPasteWrappers(text) {
  if (!text) {
    return text
  }

  return text
    .replace(/\x1b\[200~/g, '')
    .replace(/\x1b\[201~/g, '')
    .replace(/\^\[\[200~/g, '')
    .replace(/\^\[\[201~/g, '')
    .replace(BRACKETED_PASTE_BOUNDARY_START, '$1')
    .replace(BRACKETED_PASTE_BOUNDARY_END, '')
    .replace(BRACKETED_PASTE_DEGRADED_START, '$1')
    .replace(BRACKETED_PASTE_DEGRADED_END, '')
}

export function collapseRepeatedInputArtifacts(text, minRepeats = 4) {
  if (!text) {
    return text
  }

  let index = text.length
  let repeatCount = 0

  while (
    index >= DESKTOP_PASTE_ARTIFACT.length &&
    text.slice(index - DESKTOP_PASTE_ARTIFACT.length, index) ===
      DESKTOP_PASTE_ARTIFACT
  ) {
    repeatCount += 1
    index -= DESKTOP_PASTE_ARTIFACT.length
  }

  if (repeatCount < minRepeats) {
    return text
  }

  let start = index

  if (start >= 2 && text.slice(start - 2, start) === '[e') {
    start -= 2
  } else if (start >= 1 && text[start - 1] === '[') {
    start -= 1
  }

  return text.slice(0, start)
}

export function sanitizeComposerInput(text) {
  if (!text) {
    return text
  }

  return collapseRepeatedInputArtifacts(
    stripLeakedBracketedPasteWrappers(text)
  )
}

function quoteRefValue(value) {
  if (!value.includes('`')) {
    return `\`${value}\``
  }
  if (!value.includes('"')) {
    return `"${value}"`
  }
  if (!value.includes("'")) {
    return `'${value}'`
  }
  return value
}

export function barePathRef(path) {
  const trimmed = path.replace(/\/+$/, '')

  return trimmed
    ? `@${path.endsWith('/') ? 'folder' : 'file'}:${quoteRefValue(trimmed)}`
    : null
}

export function pathifyRefs(text) {
  if (!text.includes('@')) {
    return text
  }

  REF_RE.lastIndex = 0
  const fenced = Array.from(text.matchAll(REF_RE)).map(match => {
    const start = match.index ?? 0

    return { end: start + match[0].length, start }
  })
  let out = ''
  let cursor = 0

  for (const match of text.matchAll(BARE_PATH_RE)) {
    const start = match.index ?? 0
    const ref = barePathRef(match[1] || '')

    if (!ref || fenced.some(span => start >= span.start && start < span.end)) {
      continue
    }

    out += `${text.slice(cursor, start)}${ref}`
    cursor = start + match[0].length
  }

  return out + text.slice(cursor)
}

/** Exact text sent on the prompt.submit wire for a plain-text composer send. */
export function canonicalComposerSubmitText(text) {
  return sanitizeComposerInput(pathifyRefs(text)).trim()
}
