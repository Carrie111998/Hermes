import { useEffect, useState } from 'react'

import { StableText } from '@/components/chat/stable-text'
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

export type ContextCompressionPressure = 'normal' | 'near' | 'due'

const CONTEXT_COMPRESSION_WARNING_RATIO = 0.85

export function contextTokensUntilCompression(usage: UsageStats): number | null {
  const threshold = usage.compression_threshold_tokens
  const used = usage.context_used

  if (!threshold || threshold <= 0 || used == null || used < 0) {
    return null
  }

  return Math.max(0, threshold - used)
}

export function contextCompressionPressure(usage: UsageStats): ContextCompressionPressure {
  const threshold = usage.compression_threshold_tokens
  const used = usage.context_used

  if (!threshold || threshold <= 0 || used == null || used <= 0) {
    return 'normal'
  }

  if (used >= threshold) {
    return 'due'
  }

  return used >= threshold * CONTEXT_COMPRESSION_WARNING_RATIO ? 'near' : 'normal'
}

export function LiveDuration({ since }: { since: number | null | undefined }) {
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!since) {
      return
    }

    const tick = () => setNow(Date.now())
    tick()
    const timer = window.setInterval(tick, 1000)

    return () => window.clearInterval(timer)
  }, [since])

  if (!since) {
    return null
  }

  return <StableText>{formatDuration(now - since)}</StableText>
}
