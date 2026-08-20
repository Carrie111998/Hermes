import { useState } from 'react'

import { StableText } from '@/components/chat/stable-text'
import { useViewedInterval } from '@/hooks/use-viewed-interval'
import { compactNumber } from '@/lib/format'
import type { UsageStats } from '@/types/hermes'

export function formatDuration(elapsedMs: number): string {
  const totalSeconds = Math.max(0, Math.floor(elapsedMs / 1000))
  const seconds = totalSeconds % 60
  const minutes = Math.floor(totalSeconds / 60) % 60
  const hours = Math.floor(totalSeconds / 3600)
  const ss = String(seconds).padStart(2, '0')
  const mm = String(minutes).padStart(2, '0')

  return hours > 0 ? `${hours}:${mm}:${ss}` : `${minutes}:${ss}`
}

export function compactPath(path: string, max = 44): string {
  const trimmed = path.trim()

  if (trimmed.length <= max) {
    return trimmed
  }

  const segments = trimmed.split('/').filter(Boolean)

  if (segments.length < 2) {
    return `…${trimmed.slice(-(max - 1))}`
  }

  const tail = segments.slice(-2).join('/')

  return tail.length + 2 >= max ? `…${tail.slice(-(max - 1))}` : `…/${tail}`
}

export function contextBar(percent: number | undefined, width = 10): string {
  const bounded = Math.max(0, Math.min(100, percent ?? 0))
  const filled = Math.round((bounded / 100) * width)

  return `${'█'.repeat(filled)}${'░'.repeat(width - filled)}`
}

export function usageContextLabel(usage: UsageStats): string {
  if (usage.context_max) {
    return `${compactNumber(usage.context_used ?? 0)}/${compactNumber(usage.context_max)}`
  }

  return usage.total > 0 ? `${compactNumber(usage.total)} tok` : ''
}

export function contextBarLabel(usage: UsageStats): string {
  if (!usage.context_max) {
    return ''
  }

  const pct = Math.max(0, Math.min(100, Math.round(usage.context_percent ?? 0)))

  return `[${contextBar(usage.context_percent)}] ${pct}%`
}

export function tokPerCallLabel(usage: UsageStats): string {
  // WHY: a missing or zero rate means the backend has not finished an API call
  // yet, so render nothing and let the pill stay hidden instead of showing a
  // fabricated 0.0.
  if (!usage.tok_per_call) {
    return ''
  }

  return `call ${usage.tok_per_call.toFixed(1)} tok/s`
}

export function tokPerTurnLabel(usage: UsageStats): string {
  // WHY: same rule as tokPerCallLabel, hide the pill until a turn has
  // completed and produced a real per-turn rate.
  if (!usage.tok_per_turn) {
    return ''
  }

  return `turn ${usage.tok_per_turn.toFixed(1)} tok/s`
}

export function LiveDuration({ since }: { since: number | null | undefined }) {
  const [now, setNow] = useState(() => Date.now())

  useViewedInterval(() => setNow(Date.now()), 1000, Boolean(since))

  if (!since) {
    return null
  }

  return <StableText>{formatDuration(now - since)}</StableText>
}
