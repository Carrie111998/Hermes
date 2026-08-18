import React from 'react'
import { Box, Text } from '@hermes/ink'
import { useStore } from '@nanostores/react'
import { $uiState, $uiTheme } from '../app/uiStore.js'
import type { SubagentTab } from '../app/interfaces.js'

export interface SubagentTabBarProps {
  onSelectSubagent: (subagentSid: string) => void
}

export function SubagentTabBar({ onSelectSubagent }: SubagentTabBarProps) {
  const ui = useStore($uiState)
  const theme = useStore($uiTheme)
  const subagents = ui.activeSubagents || []

  if (subagents.length === 0) {
    return null
  }

  return (
    <Box flexDirection="row" flexWrap="wrap" marginTop={1} paddingX={1}>
      <Text color={theme.color.muted}>{'Subagents: '}</Text>
      {subagents.map((agent: SubagentTab) => {
        const isRunning = agent.status === 'running'
        const badgeColor = isRunning ? theme.color.accent : theme.color.muted
        const label = ` 🤖 ${agent.name} ${isRunning ? '●' : '✓'} `

        return (
          <Box
            key={agent.id}
            borderStyle="single"
            borderColor={badgeColor}
            marginRight={1}
            onClick={() => onSelectSubagent(agent.id)}
          >
            <Text color={badgeColor} bold={isRunning}>
              {label}
            </Text>
          </Box>
        )
      })}
    </Box>
  )
}
