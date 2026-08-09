import { classifyRemotePreviewTarget } from './remote-preview-classifier'

export function normalizeRemotePreviewTarget(rawTarget: string, rewrittenTarget: string | null | undefined) {
  const raw = String(rawTarget || '').trim()
  const rewritten = String(rewrittenTarget || '').trim()

  if (!raw || !rewritten) {
    return null
  }

  const classification = classifyRemotePreviewTarget(rewritten)

  if (!classification?.isHttp || classification.isLocal) {
    return null
  }

  return {
    kind: 'url' as const,
    label: `${classification.url.host}${classification.url.pathname === '/' ? '' : classification.url.pathname}`,
    source: raw,
    url: rewritten
  }
}
