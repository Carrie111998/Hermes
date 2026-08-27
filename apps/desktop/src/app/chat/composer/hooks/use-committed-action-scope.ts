import { useCallback, useLayoutEffect, useRef } from 'react'

/**
 * Commit-safe lifecycle generation for session-bound actions.
 *
 * A work-in-progress React render must never mutate refs read by the currently
 * committed UI. We calculate the generation the candidate render will own, but
 * publish it only from a layout effect. If React abandons the render, visible
 * handlers keep matching the last committed generation.
 */
export function useCommittedActionScope(scopeKey: string | null, disabled: boolean): () => boolean {
  const committedRef = useRef({ disabled, epoch: 0, key: scopeKey })

  const renderEpoch =
    committedRef.current.key === scopeKey ? committedRef.current.epoch : committedRef.current.epoch + 1

  useLayoutEffect(() => {
    const current = committedRef.current
    const epoch = current.key === scopeKey ? current.epoch : Math.max(renderEpoch, current.epoch + 1)

    committedRef.current = { disabled, epoch, key: scopeKey }
  }, [disabled, renderEpoch, scopeKey])

  return useCallback(() => {
    const committed = committedRef.current

    return committed.key === scopeKey && committed.epoch === renderEpoch && !committed.disabled
  }, [renderEpoch, scopeKey])
}
