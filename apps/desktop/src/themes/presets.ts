/**
 * Built-in desktop themes. Names match the CLI skins / dashboard presets.
 * Add new themes here — no code changes needed elsewhere.
 */

import type { DesktopTheme, DesktopThemeTypography } from './types'

// Color-emoji fonts to append to every stack as a last resort. None of the UI
// text/mono fonts carry emoji glyphs, so without this emoji render as tofu
// boxes on platforms whose default text font lacks them (e.g. Linux/#40364).
// Covers macOS, Windows, Linux, plus the `emoji` generic for anything else.
export const EMOJI_FALLBACK = '"Apple Color Emoji", "Segoe UI Emoji", "Segoe UI Symbol", "Noto Color Emoji", emoji'

const SYSTEM_SANS =
  '"Segoe WPC", "Segoe UI", -apple-system, BlinkMacSystemFont, "SF Pro Text", "SF Pro Display", system-ui, sans-serif, ' +
  EMOJI_FALLBACK

const SYSTEM_MONO =
  '"Cascadia Code", "JetBrains Mono", "SF Mono", ui-monospace, Menlo, Monaco, Consolas, monospace, ' + EMOJI_FALLBACK

export const DEFAULT_TYPOGRAPHY: DesktopThemeTypography = { fontSans: SYSTEM_SANS, fontMono: SYSTEM_MONO }

/** 
 * Nous — canonical Hermes desktop identity. 
 * Lyons Command Center brand: navy #05060A, gold #C9A844, gold-light #E8C96A
 */
export const nousTheme: DesktopTheme = {
  name: 'nous',
  label: 'Nous',
  description: 'Lyons Command Center: navy & gold',
  colors: {
    background: '#05060A',
    foreground: '#E8C96A',
    card: '#0A0B0F',
    cardForeground: '#E8C96A',
    muted: '#1A1D24',
    mutedForeground: '#8B7D5A',
    popover: '#0A0B0F',
    popoverForeground: '#E8C96A',
    primary: '#C9A844',
    primaryForeground: '#05060A',
    secondary: '#0F1118',
    secondaryForeground: '#C9A844',
    accent: '#E8C96A',
    accentForeground: '#05060A',
    border: '#1F2330',
    input: '#0D0F14',
    ring: '#C9A844',
    midground: '#C9A844',
    composerRing: '#C9A844',
    destructive: '#C75050',
    destructiveForeground: '#05060A',
    sidebarBackground: '#0C0E14',
    sidebarBorder: '#1A1D28',
    userBubble: '#141720',
    userBubbleBorder: '#2A2E3A'
  },
  darkColors: {
    background: '#05060A',
    foreground: '#E8C96A',
    card: '#0A0B0F',
    cardForeground: '#E8C96A',
    muted: '#1A1D24',
    mutedForeground: '#8B7D5A',
    popover: '#0A0B0F',
    popoverForeground: '#E8C96A',
    primary: '#C9A844',
    primaryForeground: '#05060A',
    secondary: '#0F1118',
    secondaryForeground: '#C9A844',
    accent: '#E8C96A',
    accentForeground: '#05060A',
    border: '#1F2330',
    input: '#0D0F14',
    ring: '#C9A844',
    midground: '#C9A844',
    composerRing: '#E8C96A',
    destructive: '#C75050',
    destructiveForeground: '#05060A',
    sidebarBackground: '#0C0E14',
    sidebarBorder: '#1A1D28',
    userBubble: '#141720',
    userBubbleBorder: '#2A2E3A'
  },
  typography: {
    fontSans: SYSTEM_SANS,
    fontMono: SYSTEM_MONO
  }
}

/** Deep blue-violet with cool accents. Matches the dashboard midnight theme. */
export const midnightTheme: DesktopTheme = {
  name: 'midnight',
  label: 'Midnight',
  description: 'Deep blue-violet with cool accents',
  colors: {
    background: '#08081c',
    foreground: '#ddd6ff',
    card: '#0d0d28',
    cardForeground: '#ddd6ff',
    muted: '#13133a',
    mutedForeground: '#7c7ab0',
    popover: '#0f0f2e',
    popoverForeground: '#ddd6ff',
    primary: '#ddd6ff',
    primaryForeground: '#08081c',
    secondary: '#1a1a4a',
    secondaryForeground: '#c4bff0',
    accent: '#1a1a44',
    accentForeground: '#d0c8ff',
    border: '#1e1e52',
    input: '#1e1e52',
    ring: '#8b80e8',
    midground: '#8b80e8',
    destructive: '#b03060',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#06061a',
    sidebarBorder: '#12123a',
    userBubble: '#14143a',
    userBubbleBorder: '#242466'
  },
  darkColors: {
    background: '#08081c',
    foreground: '#ddd6ff',
    card: '#0d0d28',
    cardForeground: '#ddd6ff',
    muted: '#13133a',
    mutedForeground: '#7c7ab0',
    popover: '#0f0f2e',
    popoverForeground: '#ddd6ff',
    primary: '#ddd6ff',
    primaryForeground: '#08081c',
    secondary: '#1a1a4a',
    secondaryForeground: '#c4bff0',
    accent: '#1a1a44',
    accentForeground: '#d0c8ff',
    border: '#1e1e52',
    input: '#1e1e52',
    ring: '#8b80e8',
    midground: '#8b80e8',
    destructive: '#b03060',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#06061a',
    sidebarBorder: '#12123a',
    userBubble: '#14143a',
    userBubbleBorder: '#242466'
  },
  typography: {
    fontSans: SYSTEM_SANS,
    fontMono: SYSTEM_MONO
  }
}

/** Lean monochrome. For terminals, terminals, and terminals. */
export const monoTheme: DesktopTheme = {
  name: 'mono',
  label: 'Mono',
  description: 'Lean monochrome',
  colors: {
    background: '#0b0b0b',
    foreground: '#d4d4d4',
    card: '#161616',
    cardForeground: '#d4d4d4',
    muted: '#2a2a2a',
    mutedForeground: '#8c8c8c',
    popover: '#1e1e1e',
    popoverForeground: '#d4d4d4',
    primary: '#d4d4d4',
    primaryForeground: '#0b0b0b',
    secondary: '#1c1c1c',
    secondaryForeground: '#a0a0a0',
    accent: '#373737',
    accentForeground: '#d4d4d4',
    border: '#3a3a3a',
    input: '#1c1c1c',
    ring: '#d4d4d4',
    midground: '#3d3d3d',
    destructive: '#c72e4d',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#0d0d0d',
    sidebarBorder: '#2a2a2a',
    userBubble: '#1a1a1a',
    userBubbleBorder: '#323232'
  },
  darkColors: {
    background: '#0b0b0b',
    foreground: '#d4d4d4',
    card: '#161616',
    cardForeground: '#d4d4d4',
    muted: '#2a2a2a',
    mutedForeground: '#8c8c8c',
    popover: '#1e1e1e',
    popoverForeground: '#d4d4d4',
    primary: '#d4d4d4',
    primaryForeground: '#0b0b0b',
    secondary: '#1c1c1c',
    secondaryForeground: '#a0a0a0',
    accent: '#373737',
    accentForeground: '#d4d4d4',
    border: '#3a3a3a',
    input: '#1c1c1c',
    ring: '#d4d4d4',
    midground: '#3d3d3d',
    destructive: '#c72e4d',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#0d0d0d',
    sidebarBorder: '#2a2a2a',
    userBubble: '#1a1a1a',
    userBubbleBorder: '#323232'
  },
  typography: {
    fontSans: SYSTEM_SANS,
    fontMono: SYSTEM_MONO
  }
}

/** Cool blue developer-focused theme. */
export const slateTheme: DesktopTheme = {
  name: 'slate',
  label: 'Slate',
  description: 'Cool blue developer-focused theme',
  colors: {
    background: '#0f1117',
    foreground: '#c9d1d9',
    card: '#161b22',
    cardForeground: '#c9d1d9',
    muted: '#21262d',
    mutedForeground: '#8b949e',
    popover: '#1c2128',
    popoverForeground: '#c9d1d9',
    primary: '#58a6ff',
    primaryForeground: '#000b15',
    secondary: '#1c2128',
    secondaryForeground: '#8b949e',
    accent: '#1f6feb',
    accentForeground: '#e6edf3',
    border: '#30363d',
    input: '#0d1117',
    ring: '#388bfd',
    midground: '#484f58',
    destructive: '#f85149',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#010101',
    sidebarBorder: '#21262d',
    userBubble: '#2d3748',
    userBubbleBorder: '#444c56'
  },
  darkColors: {
    background: '#0f1117',
    foreground: '#c9d1d9',
    card: '#161b22',
    cardForeground: '#c9d1d9',
    muted: '#21262d',
    mutedForeground: '#8b949e',
    popover: '#1c2128',
    popoverForeground: '#c9d1d9',
    primary: '#58a6ff',
    primaryForeground: '#000b15',
    secondary: '#1c2128',
    secondaryForeground: '#8b949e',
    accent: '#1f6feb',
    accentForeground: '#e6edf3',
    border: '#30363d',
    input: '#0d1117',
    ring: '#388bfd',
    midground: '#484f58',
    destructive: '#f85149',
    destructiveForeground: '#fef2f2',
    sidebarBackground: '#010101',
    sidebarBorder: '#21262d',
    userBubble: '#2d3748',
    userBubbleBorder: '#444c56'
  },
  typography: {
    fontSans: SYSTEM_SANS,
    fontMono: SYSTEM_MONO
  }
}

export const BUILTIN_THEMES: Record<string, DesktopTheme> = {
  nous: nousTheme,
  midnight: midnightTheme,
  mono: monoTheme,
  slate: slateTheme
}

export const BUILTIN_THEME_LIST = Object.values(BUILTIN_THEMES)

/** Skin used when nothing is persisted or the persisted name is retired. */
export const DEFAULT_SKIN_NAME = 'nous'
