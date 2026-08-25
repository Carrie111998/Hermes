function clipboardTextExtension(text: unknown) {
  const trimmed = String(text || '').trim()

  if (trimmed[0] !== '{' && trimmed[0] !== '[') {
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

function isFilenameControlCharacter(char: string) {
  const code = char.codePointAt(0)

  return code !== undefined && (code < 32 || (code >= 127 && code <= 159))
}

function composerTextFilenamePrefix(text: unknown) {
  const preview = Array.from(String(text || '').trim().replace(/\s+/g, ' '))
    .slice(0, 30)
    .join('')
    .replace(/[<>:"/\\|?*]/g, '')

  return (
    Array.from(preview)
      .filter(char => !isFilenameControlCharacter(char))
      .join('')
      .trim()
      .replace(/[. ]+$/, '') || 'clipboard'
  )
}

export { clipboardTextExtension, composerTextFilenamePrefix, hasClipboardText }
