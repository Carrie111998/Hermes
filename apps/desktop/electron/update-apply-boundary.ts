export type NativeUpdateStatus = {
  supported?: boolean
  behind?: number
  error?: string
} | null | undefined

export type NativeUpdateApplyDecision =
  | { ok: true }
  | { ok: false; error: 'update-not-applyable'; message: string }

export function nativeUpdateApplyDecision(status: NativeUpdateStatus): NativeUpdateApplyDecision {
  if (!status || status.supported !== true) {
    return {
      ok: false,
      error: 'update-not-applyable',
      message: 'Desktop updates are unavailable for this installation.'
    }
  }

  if (status.error) {
    return {
      ok: false,
      error: 'update-not-applyable',
      message: 'Desktop update status must be checked successfully before applying.'
    }
  }

  if (!Number.isInteger(status.behind) || Number(status.behind) <= 0) {
    return {
      ok: false,
      error: 'update-not-applyable',
      message: status.behind === 0 ? 'Hermes Desktop is already up to date.' : 'No verified Desktop update is available.'
    }
  }

  return { ok: true }
}
