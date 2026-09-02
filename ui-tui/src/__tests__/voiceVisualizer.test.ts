import { describe, expect, it } from 'vitest'

import {
  renderVoiceVisualization,
  renderVoiceWaveform,
  voiceVisualizationFooter
} from '../components/voiceVisualizer.js'

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

  it('renders exactly the selected visualization', () => {
    const orb = renderVoiceVisualization('orb', 19, 3, 'listening')
    const waveform = renderVoiceVisualization('waveform', 32, 3, 'listening')

    expect(orb).toHaveLength(7)
    expect(orb.every(row => row.length === 19)).toBe(true)
    expect(waveform).toHaveLength(7)
    expect(waveform.every(row => row.length === 32)).toBe(true)
  })

  it('distinguishes transport startup from active listening', () => {
    expect(voiceVisualizationFooter('waiting')).toBe('Waiting for realtime voice…')
    expect(voiceVisualizationFooter('listening')).toBe('Listening · press the voice key to stop')
  })

  it('preserves distinct solving and composing phases', () => {
    expect(voiceVisualizationFooter('solving')).toBe('Solving…')
    expect(voiceVisualizationFooter('composing')).toBe('Speaking…')
    expect(renderVoiceVisualization('orb', 19, 3, 'solving')).not.toEqual(
      renderVoiceVisualization('orb', 19, 3, 'composing')
    )
  })
})
