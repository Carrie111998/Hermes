import { Box, Text } from '@hermes/ink'
import { renderTuiOrb } from 'thinking-orbs/tui'
import { useEffect, useMemo, useState } from 'react'

import type { Theme } from '../theme.js'

export interface VoiceVisualizerProps {
  columns: number
  mode: 'listening' | 'thinking'
  t: Theme
}

const FRAME_MS = 80
const ORB_COLUMNS = 19
const ORB_ROWS = 7
const WAVE_BLOCKS = [' ', '▁', '▂', '▃', '▄', '▅', '▆', '▇', '█'] as const

export function renderVoiceWaveform(width: number, frame: number, active: boolean): string[] {
  const rows = ORB_ROWS
  const output = Array.from({ length: rows }, () => '')
  const maxHeight = rows * (WAVE_BLOCKS.length - 1)
  const energy = active ? 0.72 + Math.sin(frame * 0.17) * 0.12 : 0.24

  for (let column = 0; column < width; column += 1) {
    const carrier = 0.5 + 0.5 * Math.sin(frame * 0.43 + column * 0.71)
    const shimmer = 0.5 + 0.5 * Math.sin(frame * 0.19 - column * 1.17)
    const height = Math.round(energy * (0.3 + carrier * 0.5 + shimmer * 0.2) * maxHeight)

    for (let row = 0; row < rows; row += 1) {
      const units = Math.max(0, Math.min(WAVE_BLOCKS.length - 1, height - (rows - row - 1) * 8))
      output[row] += WAVE_BLOCKS[units]
    }
  }

  return output
}

export function VoiceVisualizer({ columns, mode, t }: VoiceVisualizerProps) {
  const [frame, setFrame] = useState(0)

  useEffect(() => {
    const timer = setInterval(() => setFrame(current => current + 1), FRAME_MS)

    return () => clearInterval(timer)
  }, [])

  const panelWidth = Math.max(24, columns - 2)
  const innerWidth = panelWidth - 2
  const orbColumns = Math.min(ORB_COLUMNS, Math.max(6, innerWidth - 2))
  const waveWidth = Math.max(0, innerWidth - orbColumns - 2)
  const active = mode === 'listening'
  const orb = useMemo(
    () =>
      renderTuiOrb(active ? 'listening' : 'solving', {
        columns: orbColumns,
        rows: ORB_ROWS,
        speed: active ? 0.72 : 0.55,
        threshold: active ? 0.18 : 0.21,
        time: frame * (FRAME_MS / 1000)
      }).lines,
    [active, frame, orbColumns]
  )
  const waveform = useMemo(() => renderVoiceWaveform(waveWidth, frame, active), [active, frame, waveWidth])

  return (
    <Box borderColor={t.color.border} borderStyle="single" flexDirection="column" width={panelWidth}>
      {orb.map((line, index) => (
        <Box key={index}>
          <Text color={t.color.accent}>{line}</Text>
          {waveWidth > 0 ? (
            <>
              <Text>{'  '}</Text>
              <Text color={active ? t.color.ok : t.color.warn}>{waveform[index]}</Text>
            </>
          ) : null}
        </Box>
      ))}
      <Text color={t.color.muted}>{active ? 'Listening · press the voice key to stop' : 'Transcribing…'}</Text>
    </Box>
  )
}
