import { describe, expect, it } from 'vitest'

import { renderVoiceWaveform } from '../components/voiceVisualizer.js'

describe('renderVoiceWaveform', () => {
  it('keeps a fixed seven-row pane and fills the requested width', () => {
    const rows = renderVoiceWaveform(32, 0, true)

    expect(rows).toHaveLength(7)
    expect(rows.every(row => row.length === 32)).toBe(true)
    expect(rows.join('')).toMatch(/[▁▂▃▄▅▆▇█]/u)
  })

  it('animates listening energy without changing pane geometry', () => {
    const initial = renderVoiceWaveform(24, 0, true)
    const advanced = renderVoiceWaveform(24, 8, true)

    expect(advanced).not.toEqual(initial)
    expect(advanced.every(row => row.length === 24)).toBe(true)
  })
})
