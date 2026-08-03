import type { BillingBlock } from '@hermes/shared'
import { atom } from 'nanostores'

import { isManagedEvaosAgent } from '@/i18n/managed-brand'
import { openExternalLink } from '@/lib/external-link'

/**
 * The active inference billing wall, if any. Set from the gateway
 * `message.complete` / `error` event when a turn fails with
 * `FailoverReason.billing` (see `agent/billing_links.py`). One global slot: a
 * credit wall on the active session's provider is the whole app's problem, and
 * the newest block wins. Cleared when a new turn starts or the user dismisses.
 */
export interface ActiveBillingBlock {
  block: BillingBlock
  sessionId: string
  at: number
}

export const $billingBlock = atom<ActiveBillingBlock | null>(null)

/**
 * Navigation intent counter. A toast fired outside React (or any surface
 * without router context) bumps this to ask the shell — which owns
 * `useNavigate` — to open Settings → Billing in-app. See `contrib/wiring.tsx`.
 */
export const $billingSettingsRequest = atom(0)

export interface BillingBlockPresentationCopy {
  fallbackMessage: string
  titleNous: string
  titleProvider: (provider: string) => string
}

export interface BillingBlockPresentation {
  message: string
  title: string
}

export function billingBlockPresentation(
  block: BillingBlock,
  managed: boolean,
  copy: BillingBlockPresentationCopy
): BillingBlockPresentation {
  if (managed) {
    return {
      title: 'evaOS Agent access unavailable',
      message: 'Contact Electric Sheep support to restore access.'
    }
  }

  return {
    title: block.is_nous ? copy.titleNous : copy.titleProvider(block.provider_label),
    message: block.message.split('\n')[0]?.trim() || copy.fallbackMessage
  }
}

export function setBillingBlock(sessionId: string, block: BillingBlock): void {
  $billingBlock.set({ at: Date.now(), block, sessionId })
}

export function clearBillingBlock(sessionId?: string): void {
  const current = $billingBlock.get()

  if (!current) {
    return
  }

  // A scoped clear (new turn on session X) must not wipe a block raised by a
  // different session's provider.
  if (sessionId && current.sessionId !== sessionId) {
    return
  }

  $billingBlock.set(null)
}

export function requestBillingSettings(): void {
  $billingSettingsRequest.set($billingSettingsRequest.get() + 1)
}

export function billingRecoveryAvailable(_block: BillingBlock): boolean {
  return !isManagedEvaosAgent()
}

/**
 * The single recovery action for a billing wall, shared by the toast and the
 * in-chat banner so both behave identically: Nous routes to the upstream
 * Settings → Billing surface; a third-party provider deep-links to its own
 * billing page (falling back to the in-app surface only if we have no URL).
 * Managed evaOS Agent exposes no billing action; Electric Sheep owns account
 * recovery outside the upstream provider surfaces.
 */
export function runBillingRecovery(block: BillingBlock): void {
  if (!billingRecoveryAvailable(block)) {
    return
  }

  if (block.is_nous) {
    requestBillingSettings()

    return
  }

  if (block.billing_url) {
    openExternalLink(block.billing_url)

    return
  }

  requestBillingSettings()
}

export function billingCtaLabel(block: BillingBlock, copy: { addCredits: string; openBilling: string }): string {
  return block.is_nous ? copy.openBilling : copy.addCredits
}
