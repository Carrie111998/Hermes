import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'

import { ErrorBoundary } from '@/components/error-boundary'

import { ScreenAnnotationsApp } from './annotations-app'

/**
 * Boot the screen-annotation overlay window. Loaded by the same bundle as the
 * main app but via `?win=annotate`, so it shares build plumbing while mounting
 * a minimal, transparent surface (no app shell, no gateway, no theme — the
 * marks use fixed screen-legible colors).
 *
 * The index.html boot script paints an OPAQUE themed background to avoid a
 * flash in normal windows; this overlay must be see-through, so we force every
 * host layer transparent with a late, high-specificity style tag (same trick
 * as the pet overlay).
 */
export function mountScreenAnnotations(): void {
  const style = document.createElement('style')
  style.textContent = 'html,body,#root{background:transparent !important;}'
  document.head.appendChild(style)

  const root = document.getElementById('root')

  if (!root) {
    return
  }

  createRoot(root).render(
    <StrictMode>
      <ErrorBoundary label="screen-annotations">
        <ScreenAnnotationsApp />
      </ErrorBoundary>
    </StrictMode>
  )
}
