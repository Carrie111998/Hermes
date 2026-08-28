// Clipboard text is user supplied and can be much larger than a normal paste.
// Avoid parsing huge JSON blobs on Electron's main process, and bound the
// persisted composer-file cache separately.
const MAX_CLIPBOARD_TEXT_JSON_BYTES = 2 * 1024 * 1024
const MAX_COMPOSER_CLIPBOARD_TEXT_BYTES = 10 * 1024 * 1024

type ClipboardTextSaveResult =
  { status: 'saved'; path: string } | { status: 'empty' } | { status: 'image' } | { status: 'too_large' }

function clipboardTextByteLength(text: unknown) {
  return Buffer.byteLength(String(text || ''), 'utf8')
}

function clipboardTextExtension(text: unknown) {
  const trimmed = String(text || '').trim()

  if ((trimmed[0] !== '{' && trimmed[0] !== '[') || clipboardTextByteLength(trimmed) > MAX_CLIPBOARD_TEXT_JSON_BYTES) {
    return '.md'
  }

  try {
    const value = JSON.parse(trimmed)

    return Array.isArray(value) || (value !== null && typeof value === 'object') ? '.json' : '.md'
  } catch {
    return '.md'
  }
}

function hasClipboardText(text: unknown) {
  return Boolean(String(text || '').trim())
}

function isClipboardTextTooLarge(text: unknown) {
  return clipboardTextByteLength(text) > MAX_COMPOSER_CLIPBOARD_TEXT_BYTES
}

function isFilenameControlCharacter(char: string) {
  const code = char.codePointAt(0)

  return code !== undefined && (code < 32 || (code >= 127 && code <= 159))
}

function composerTextFilenamePrefix(text: unknown) {
  const normalized = String(text || '')
    .trim()
    .replace(/\s+/g, ' ')

  // Take at most 30 code points without materializing the whole clipboard:
  // for...of yields full code points (never splits a surrogate pair) and
  // stops early, so a huge paste only scans its first 30 characters.
  const previewChars: string[] = []

  for (const char of normalized) {
    if (previewChars.length >= 30) {
      break
    }

    previewChars.push(char)
  }

  return (
    previewChars
      .filter(char => !isFilenameControlCharacter(char))
      .join('')
      .replace(/[<>:"/\\|?*]/g, '')
      .trim()
      .replace(/[. ]+$/, '') || 'clipboard'
  )
}

export {
  clipboardTextExtension,
  type ClipboardTextSaveResult,
  composerTextFilenamePrefix,
  hasClipboardText,
  isClipboardTextTooLarge,
  MAX_CLIPBOARD_TEXT_JSON_BYTES,
  MAX_COMPOSER_CLIPBOARD_TEXT_BYTES
}
