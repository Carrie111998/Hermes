/**
 * Shared terminal/env flag parsing.
 *
 * Consolidates the four previously-duplicated truthy/falsy regexes
 * (forceTruecolor.ts, config/env.ts, termux.ts, theme.ts) so they cannot
 * drift apart. Every call site already trims (and, where relevant,
 * lowercases) its input before testing, so a single case-insensitive
 * definition is behavior-preserving across all of them.
 */
const TRUE_RE = /^(?:1|true|yes|on)$/i
const FALSE_RE = /^(?:0|false|no|off)$/i

export const truthy = (value?: string): boolean => TRUE_RE.test(String(value ?? '').trim())

export const falsy = (value?: string): boolean => FALSE_RE.test(String(value ?? '').trim())
