import { useEffect, useRef, useState } from 'react'

import { triggerHaptic } from '@/lib/haptics'

interface UseComposerUrlDialogOptions {
  disabled: boolean
  insertText: (text: string) => void
  onAddUrl?: (url: string) => void
  scopeKey: string | null
}

/**
 * "Add URL" dialog engine: open/value state, autofocus-on-open, and submit. On
 * submit it prefers the host's `onAddUrl` (which may fetch/title the link) and
 * otherwise drops an `@url:` directive into the draft.
 */
export function useComposerUrlDialog({ disabled, insertText, onAddUrl, scopeKey }: UseComposerUrlDialogOptions) {
  const urlInputRef = useRef<HTMLInputElement | null>(null)
  const [urlOpen, setUrlOpen] = useState(false)
  const [urlValue, setUrlValue] = useState('')
  const scopeEpochRef = useRef({ key: scopeKey, value: 0 })

  if (scopeEpochRef.current.key !== scopeKey) {
    scopeEpochRef.current = { key: scopeKey, value: scopeEpochRef.current.value + 1 }
  }

  const renderScopeEpoch = scopeEpochRef.current.value
  const disabledRef = useRef(disabled)

  disabledRef.current = disabled
  const actionIsCurrent = () => scopeEpochRef.current.value === renderScopeEpoch && !disabledRef.current

  useEffect(() => {
    setUrlOpen(false)
    setUrlValue('')
  }, [scopeKey])

  useEffect(() => {
    if (urlOpen) {
      window.requestAnimationFrame(() => urlInputRef.current?.focus({ preventScroll: true }))
    }
  }, [urlOpen])

  const openUrlDialog = () => {
    if (!actionIsCurrent()) {
      return
    }

    triggerHaptic('open')
    setUrlOpen(true)
  }

  const submitUrl = () => {
    if (!actionIsCurrent()) {
      return
    }

    const url = urlValue.trim()

    if (!url) {
      return
    }

    if (onAddUrl) {
      onAddUrl(url)
    } else {
      insertText(`@url:${url}`)
    }

    triggerHaptic('success')
    setUrlValue('')
    setUrlOpen(false)
  }

  return { openUrlDialog, setUrlOpen, setUrlValue, submitUrl, urlInputRef, urlOpen, urlValue }
}
