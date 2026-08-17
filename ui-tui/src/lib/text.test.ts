import { describe, expect, it } from 'vitest'

import {
  formatAbandonedClarify,
  formatToolCall,
  stripTrailingPasteNewlines,
  TOOL_ICON_FALLBACK,
  toolTrailBaseLabel,
  toolTrailLabel
} from './text.js'

describe('stripTrailingPasteNewlines', () => {
  it('removes trailing newline runs from pasted text', () => {
    expect(stripTrailingPasteNewlines('alpha\n')).toBe('alpha')
    expect(stripTrailingPasteNewlines('alpha\nbeta\n\n')).toBe('alpha\nbeta')
  })

  it('preserves interior newlines', () => {
    expect(stripTrailingPasteNewlines('alpha\nbeta\ngamma')).toBe('alpha\nbeta\ngamma')
  })

  it('preserves newline-only pastes', () => {
    expect(stripTrailingPasteNewlines('\n\n')).toBe('\n\n')
  })
})

describe('formatAbandonedClarify', () => {
  it('renders the question, numbered options, and reason', () => {
    const out = formatAbandonedClarify('How do you want to scope?', ['Option A', 'Option B', 'Option C'], 'timed out')

    expect(out).toBe(
      [
        'ask How do you want to scope?',
        '  1. Option A',
        '  2. Option B',
        '  3. Option C',
        '  (timed out — no selection)'
      ].join('\n')
    )
  })

  it('handles a prompt with no choices (free-text clarify)', () => {
    const out = formatAbandonedClarify('What is the target branch?', null, 'cancelled')

    expect(out).toBe(['ask What is the target branch?', '  (cancelled — no selection)'].join('\n'))
  })

  it('trims surrounding whitespace on the question', () => {
    const out = formatAbandonedClarify('  trailing space  ', [], 'timed out')

    expect(out.split('\n')[0]).toBe('ask trailing space')
  })

  it('numbers options 1-based to match the live ClarifyPrompt', () => {
    const out = formatAbandonedClarify('q', ['first'], 'timed out')

    expect(out).toContain('  1. first')
    expect(out).not.toContain('  0.')
  })
})

describe('toolTrailBaseLabel', () => {
  // The completed trail row is a string; this is the inverse that recovers the
  // key its icon was filed under at tool.start. Asserted as a round trip so it
  // can never drift from formatToolCall's own shape.
  it('round-trips every formatToolCall shape back to the trail label', () => {
    for (const name of ['terminal', 'read_file', 'skill_view']) {
      const label = toolTrailLabel(name)

      expect(toolTrailBaseLabel(formatToolCall(name))).toBe(label)
      expect(toolTrailBaseLabel(formatToolCall(name, 'some argument'))).toBe(label)
    }
  })

  it('strips the duration the trail line appends', () => {
    expect(toolTrailBaseLabel('Terminal("ls -la") (0.4s)')).toBe('Terminal')
    expect(toolTrailBaseLabel('Read File (12.5s)')).toBe('Read File')
  })

  it('is not confused by parentheses inside the preview', () => {
    expect(toolTrailBaseLabel(formatToolCall('terminal', 'echo ("hi") && ls'))).toBe('Terminal')
  })
})

describe('TOOL_ICON_FALLBACK', () => {
  it('matches the default agent.display.get_tool_emoji falls back to', () => {
    // Both halves of the wire contract must degrade to the same glyph — a tool
    // row is never drawn with a generic bullet on either side.
    expect(TOOL_ICON_FALLBACK).toBe('⚡')
  })
})
