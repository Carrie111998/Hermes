import type { CSSProperties } from 'react'

import { getBaseColors } from '@/themes/context'

type UsageDarkStyle = CSSProperties & Record<`--${string}`, string>

export function usageDarkStyle(themeName: string): UsageDarkStyle {
  const colors = getBaseColors(themeName, 'dark')
  const accent = colors.midground ?? colors.primary

  // Usage is deliberately midnight-black in every skin; the active skin still supplies
  // foreground, semantic accents, status colors, and focus treatment.
  const midnight = {
    background: '#05070a',
    border: '#24313c',
    card: '#080c11',
    elevated: '#0b1118',
    hover: '#111a23',
    input: '#1d2a36',
    muted: '#101720',
    mutedForeground: '#94a3b2',
    secondary: '#0d141c'
  }

  return {
    '--background': midnight.background,
    '--chrome-action-hover': midnight.hover,
    '--dt-accent': midnight.hover,
    '--dt-accent-foreground': colors.accentForeground,
    '--dt-background': midnight.background,
    '--dt-border': midnight.border,
    '--dt-card': midnight.card,
    '--dt-card-foreground': colors.cardForeground,
    '--dt-destructive': colors.destructive,
    '--dt-destructive-foreground': colors.destructiveForeground,
    '--dt-foreground': colors.foreground,
    '--dt-input': midnight.input,
    '--dt-midground': accent,
    '--dt-muted': midnight.muted,
    '--dt-muted-foreground': midnight.mutedForeground,
    '--dt-popover': midnight.elevated,
    '--dt-popover-foreground': colors.popoverForeground,
    '--dt-primary': colors.primary,
    '--dt-primary-foreground': colors.primaryForeground,
    '--dt-ring': colors.ring,
    '--dt-secondary': midnight.secondary,
    '--dt-secondary-foreground': colors.secondaryForeground,
    '--foreground': colors.foreground,
    '--sidebar': midnight.background,
    '--sidebar-border': midnight.border,
    '--sidebar-foreground': colors.foreground,
    '--theme-accent-soft': midnight.hover,
    '--theme-background-seed': midnight.background,
    '--theme-bubble-seed': midnight.card,
    '--theme-card-seed': midnight.card,
    '--theme-elevated-seed': midnight.elevated,
    '--theme-foreground': colors.foreground,
    '--theme-midground': accent,
    '--theme-neutral-card': midnight.card,
    '--theme-neutral-chrome': midnight.background,
    '--theme-neutral-sidebar': midnight.background,
    '--theme-primary': colors.primary,
    '--theme-secondary': colors.secondary,
    '--ui-accent': midnight.hover,
    '--ui-accent-secondary': colors.primary,
    '--ui-base': colors.foreground,
    '--ui-bg-card': midnight.card,
    '--ui-bg-chrome': midnight.background,
    '--ui-bg-editor': midnight.card,
    '--ui-bg-elevated': midnight.elevated,
    '--ui-bg-input': midnight.card,
    '--ui-bg-primary': midnight.hover,
    '--ui-bg-quaternary': midnight.muted,
    '--ui-bg-quinary': midnight.muted,
    '--ui-bg-secondary': midnight.secondary,
    '--ui-bg-sidebar': midnight.background,
    '--ui-bg-tertiary': midnight.muted,
    '--ui-chat-surface-background': midnight.background,
    '--ui-control-active-background': midnight.secondary,
    '--ui-control-hover-background': midnight.hover,
    '--ui-editor-surface-background': midnight.background,
    '--ui-row-active-background': midnight.secondary,
    '--ui-row-hover-background': midnight.hover,
    '--ui-stroke-primary': midnight.input,
    '--ui-stroke-quaternary': midnight.border,
    '--ui-stroke-secondary': midnight.border,
    '--ui-stroke-tertiary': midnight.border,
    '--ui-surface-background': midnight.card,
    '--ui-text-primary': colors.foreground,
    '--ui-text-quaternary': midnight.mutedForeground,
    '--ui-text-secondary': colors.secondaryForeground,
    '--ui-text-tertiary': midnight.mutedForeground,
    background: midnight.background,
    color: colors.foreground,
    colorScheme: 'dark'
  }
}
