import { Box, stringWidth, Text } from '@hermes/ink'
import type { ReactNode } from 'react'
import React from 'react'

import { stableComposerColumns } from '../lib/inputMetrics.js'
import type { Theme } from '../theme.js'

export const composerOuterMargin = (cols: number) => {
  const width = Math.trunc(cols)

  if (width >= 80) {
    return 2
  }

  if (width >= 32) {
    return 1
  }

  return 0
}

export const composerSurfaceWidth = (cols: number) =>
  Math.max(1, Math.trunc(cols) - 2 - composerOuterMargin(cols) * 2)

export const composerSurfaceColumns = (cols: number, promptWidth: number, termuxMode = false) =>
  stableComposerColumns(Math.max(1, composerSurfaceWidth(cols) - 3), promptWidth, termuxMode)

const BLOCKED_FOOTER_LABEL = '· draft preserved'

export const composerFooterColumns = (cols: number, blocked: boolean) =>
  Math.max(1, composerSurfaceWidth(cols) - 5 - (blocked ? stringWidth(` ${BLOCKED_FOOTER_LABEL}`) : 0))

export function ComposerSurface({
  blocked,
  children,
  cols,
  footer,
  shell,
  t
}: {
  blocked: boolean
  children: ReactNode
  cols: number
  footer?: ReactNode | ((columns: number) => ReactNode)
  shell: boolean
  t: Theme
}) {
  const accent = blocked ? t.color.border : shell ? t.color.shellDollar : t.color.prompt
  const footerColumns = composerFooterColumns(cols, blocked)
  const footerNode = typeof footer === 'function' ? footer(footerColumns) : footer

  return (
    <Box
      backgroundColor={t.color.statusBg}
      flexDirection="row"
      marginX={composerOuterMargin(cols)}
      minHeight={5}
      width={composerSurfaceWidth(cols)}
    >
      <Box backgroundColor={accent} width={1} />
      <Box flexDirection="column" flexGrow={1} paddingX={2} paddingY={1}>
        <Box flexDirection="column">{children}</Box>
        <Box justifyContent="space-between" marginTop={1}>
          <Box flexShrink={0} height={1} overflow="hidden" width={footerColumns}>
            {footerNode ?? (
              <Text bold color={accent}>
                {shell ? 'Shell' : 'Ask Hermes'}
              </Text>
            )}
          </Box>
          {blocked && (
            <Text color={t.color.muted} dim marginLeft={1}>
              {BLOCKED_FOOTER_LABEL}
            </Text>
          )}
        </Box>
      </Box>
    </Box>
  )
}
