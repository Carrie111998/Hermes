import path from 'node:path'

import { Box, Text } from '@hermes/ink'

import type { Theme } from '../theme.js'
import type { SessionInfo } from '../types.js'

const cleanProfile = (profile?: string) =>
  profile && !['custom', 'default'].includes(profile) ? profile : ''

export function CompactWelcome({ cols, info, t }: CompactWelcomeProps) {
  const workspace = info?.cwd ? path.basename(info.cwd) || info.cwd : ''
  const model = info?.model.split('/').pop() ?? ''
  const profile = cleanProfile(info?.profile_name)
  const context = [workspace, profile, model].filter(Boolean).join(' · ')
  const narrow = cols < 50

  return (
    <Box flexDirection="column" marginBottom={1} paddingTop={1}>
      <Text bold color={t.color.primary} wrap="truncate-end">
        {t.brand.icon} {t.brand.name}
      </Text>
      {info ? (
        <Text color={t.color.muted} wrap="truncate-end">
          {context}
        </Text>
      ) : (
        <Text color={t.color.muted}>Starting session…</Text>
      )}
      <Text color={t.color.muted} wrap="truncate-end">
        {narrow ? 'Ctrl+P actions · @ context' : 'Ctrl+P actions · @ context · /help'}
      </Text>
      {info?.install_warning ? (
        <Text color={t.color.warn} wrap="truncate-end">
          ! {info.install_warning}
        </Text>
      ) : null}
      {typeof info?.update_behind === 'number' && info.update_behind > 0 ? (
        <Text color={t.color.warn} wrap="truncate-end">
          ! Update available · {info.update_behind} commits behind
        </Text>
      ) : null}
    </Box>
  )
}

interface CompactWelcomeProps {
  cols: number
  info: null | SessionInfo
  t: Theme
}
