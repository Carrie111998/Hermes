import { useStore } from '@nanostores/react'

import { GatewayProvider } from './app/gatewayContext.js'
import { $uiState } from './app/uiStore.js'
import { useMainApp } from './app/useMainApp.js'
import { AppLayout } from './components/appLayout.js'
import type { GatewayClient } from './gatewayClient.js'
import { t } from './i18n/index.js'

export function App({ gw }: { gw: GatewayClient }) {
  const { appActions, appComposer, appProgress, appStatus, appTranscript, gateway } = useMainApp(gw)
  const { mouseTracking } = useStore($uiState)

  // i18n demo — 3 видимые строки, доказывающие что инфраструктура работает.
  // При LANG=ru_RU.UTF-8 или HERMES_LANG=ru покажут русский перевод.
  const _i18nToolsTitle = t('session.availableTools')
  const _i18nSkillsTitle = t('session.availableSkills')
  const _i18nHelpTitle = t('help.quickHelp')
  void _i18nToolsTitle
  void _i18nSkillsTitle
  void _i18nHelpTitle

  return (
    <GatewayProvider value={gateway}>
      <AppLayout
        actions={appActions}
        composer={appComposer}
        mouseTracking={mouseTracking}
        progress={appProgress}
        status={appStatus}
        transcript={appTranscript}
      />
    </GatewayProvider>
  )
}
