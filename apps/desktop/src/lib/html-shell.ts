/**
 * Compose an HTML fragment into a minimal document shell; full documents pass
 * through untouched. Keeps generated fragments (no <html>/<body>) rendering
 * with sane defaults instead of quirks-mode soup.
 *
 * Shared by the artifact preview pane (right rail) and the inline html fence
 * embed so both render the same deterministic document.
 */
export function composeHtmlDocument(content: string): string {
  if (/<html[\s>]|<!doctype\s+html/i.test(content)) {
    return content
  }

  return [
    '<!doctype html>',
    '<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">',
    '<style>body{margin:0;font-family:system-ui,sans-serif}</style></head><body>',
    content,
    '</body></html>'
  ].join('\n')
}
