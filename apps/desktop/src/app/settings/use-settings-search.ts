import { useStore } from '@nanostores/react'
import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect } from 'react'

import { useGatewayRequest } from '@/app/gateway/hooks/use-gateway-request'
import { $pluginRecords } from '@/contrib/plugins-store'
import { getEnvVars, getHermesConfigSchema } from '@/hermes'
import { useI18n } from '@/i18n'
import { Package, Palette, Settings2, Wrench } from '@/lib/icons'
import { $agentPlugins, isDesktopRelevantPlugin, loadAgentPlugins } from '@/store/agent-plugins'
import { $gatewayState } from '@/store/session'
import { $settingsScopeOverride } from '@/store/settings-scope'

import { useHermesConfigRecord } from '../hooks/use-config-record'
import { useOnProfileSwitch } from '../hooks/use-on-profile-switch'

import {
  APPEARANCE_SETTING_IDS,
  buildConfigSearchEntries,
  buildCredentialSearchEntries,
  type SettingsSearchEntry
} from './settings-search'

/**
 * The granular settings-search catalog (appearance controls, config fields,
 * credentials) for the command palette's Settings page. Page destinations stay
 * on the palette side — it already owns section/page rows — this hook only
 * contributes the deep, schema-driven targets.
 */
export function useSettingsSearchCatalog(enabled: boolean) {
  const { t } = useI18n()
  // Shared settings "Applies to" scope: search the selected profile's config/
  // schema/env store, not always the active one (null → active, the default
  // path — matches every other config-backed settings page's own pattern,
  // e.g. KeysSettings' useEnvCredentials(scopeProfile)). Without this, ⌘K
  // silently searched the active profile even while the user had scoped
  // Settings to another one, so a credential/field that only exists (or is
  // only set) on the scoped profile was unfindable.
  const scopeProfile = useStore($settingsScopeOverride)
  const configQuery = useHermesConfigRecord(scopeProfile)

  const schemaQuery = useQuery({
    queryKey: ['hermes-config-schema', scopeProfile],
    queryFn: () => getHermesConfigSchema(scopeProfile),
    enabled,
    staleTime: 5 * 60 * 1000
  })

  const {
    data: envVars,
    isError: envVarsError,
    isFetching: envVarsFetching,
    refetch: refetchEnvVars
  } = useQuery({
    queryKey: ['desktop-settings-search-env-vars', scopeProfile],
    queryFn: () => getEnvVars(scopeProfile),
    enabled,
    staleTime: 5 * 60 * 1000
  })

  // scopeProfile changes are already covered by the query keys above (a new
  // key naturally refetches). This covers the OTHER staleness case: scope
  // still following the active profile (scopeProfile === null, key
  // unchanged) while the active profile itself switches underneath —
  // config/schema use the app-wide base cache key in that case, so nothing
  // else would tell them to refetch.
  const refreshCatalog = useCallback(() => {
    void configQuery.refetch()
    void schemaQuery.refetch()
    void refetchEnvVars()
    // configQuery/schemaQuery are fresh objects every render, but their
    // `refetch` re-asks the query client for that query's CURRENT key/state
    // (not a value frozen at closure-capture time), so omitting them from
    // deps doesn't risk refetching against a stale scope. Matches the
    // pre-existing refetchEnvVars-only version of this callback, which
    // useOnProfileSwitch's own contract already treats this way ("onSwitch
    // identity is intentionally ignored").
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refetchEnvVars])

  useOnProfileSwitch(refreshCatalog)

  // Plugin rows: desktop plugins are already in their store (discovered at
  // boot); agent plugins ride the gateway, so load them the first time the
  // catalog is wanted — same RPC the Plugins page fires on mount, deduped by
  // the store's own inflight guard.
  const { requestGateway } = useGatewayRequest()
  const gatewayState = useStore($gatewayState)
  const desktopPluginRecords = useStore($pluginRecords)
  const agentPlugins = useStore($agentPlugins)

  useEffect(() => {
    if (enabled && gatewayState === 'open') {
      // Same "Applies to" scope as config/schema/env above — an agent
      // plugin can be installed/enabled on one profile only, so an unscoped
      // load left it unfindable while Settings was scoped to that profile.
      void loadAgentPlugins(requestGateway, scopeProfile)
    }
  }, [enabled, gatewayState, requestGateway, scopeProfile])

  const pluginContext = t.settings.nav.plugins

  const pluginEntries: SettingsSearchEntry[] = [
    ...Object.values(desktopPluginRecords).map(record => ({
      context: pluginContext,
      description: record.description,
      icon: Package,
      id: `plugin:desktop:${record.id}`,
      keywords: ['plugin', 'extension', record.id],
      label: record.name,
      target: { plugin: record.id, view: 'plugins' as const }
    })),
    ...agentPlugins.filter(isDesktopRelevantPlugin).map(row => ({
      context: pluginContext,
      description: row.description || undefined,
      icon: Package,
      id: `plugin:agent:${row.key ?? row.name}`,
      keywords: ['plugin', 'extension', ...(row.key ? [row.key] : [])],
      label: row.name,
      target: { plugin: row.key ?? row.name, view: 'plugins' as const }
    }))
  ]

  // Never expose stale profile-scoped targets while a catalog is refreshing.
  // Field/key results wait for the current profile's data rather than briefly
  // pointing into the previous one.
  const configEntries =
    configQuery.isFetching || schemaQuery.isFetching || configQuery.isError || schemaQuery.isError
      ? []
      : buildConfigSearchEntries(schemaQuery.data?.fields, configQuery.data, {
          fieldDescriptions: t.settings.fieldDescriptions,
          fieldLabels: t.settings.fieldLabels,
          sections: t.settings.sections
        })

  const appearanceContext = t.settings.sections.appearance
  const appearance = t.settings.appearance

  const appearanceEntries: SettingsSearchEntry[] = [
    {
      context: appearanceContext,
      description: t.language.description,
      icon: Palette,
      id: `setting:${APPEARANCE_SETTING_IDS.language}`,
      keywords: ['locale'],
      label: t.language.label,
      target: { setting: APPEARANCE_SETTING_IDS.language, view: 'config:appearance' }
    },
    {
      context: appearanceContext,
      description: appearance.themeDesc,
      icon: Palette,
      id: `setting:${APPEARANCE_SETTING_IDS.theme}`,
      keywords: ['color mode', 'skin'],
      label: appearance.themeTitle,
      target: { setting: APPEARANCE_SETTING_IDS.theme, view: 'config:appearance' }
    },
    {
      context: appearanceContext,
      icon: Palette,
      id: `setting:${APPEARANCE_SETTING_IDS.uiScale}`,
      keywords: ['zoom', 'size'],
      label: appearance.uiScaleTitle,
      target: { setting: APPEARANCE_SETTING_IDS.uiScale, view: 'config:appearance' }
    },
    {
      context: appearanceContext,
      description: appearance.translucencyDesc,
      icon: Palette,
      id: `setting:${APPEARANCE_SETTING_IDS.translucency}`,
      keywords: ['opacity', 'transparent'],
      label: appearance.translucencyTitle,
      target: { setting: APPEARANCE_SETTING_IDS.translucency, view: 'config:appearance' }
    },
    {
      context: appearanceContext,
      description: appearance.backdropDesc,
      icon: Palette,
      id: `setting:${APPEARANCE_SETTING_IDS.backdrop}`,
      keywords: ['background', 'blur'],
      label: appearance.backdropTitle,
      target: { setting: APPEARANCE_SETTING_IDS.backdrop, view: 'config:appearance' }
    },
    {
      context: appearanceContext,
      description: appearance.toolViewDesc,
      icon: Palette,
      id: `setting:${APPEARANCE_SETTING_IDS.toolView}`,
      keywords: ['tool display', 'technical'],
      label: appearance.toolViewTitle,
      target: { setting: APPEARANCE_SETTING_IDS.toolView, view: 'config:appearance' }
    },
    {
      context: appearanceContext,
      description: appearance.embedsDesc,
      icon: Palette,
      id: `setting:${APPEARANCE_SETTING_IDS.embeds}`,
      keywords: ['external content', 'privacy'],
      label: appearance.embedsTitle,
      target: { setting: APPEARANCE_SETTING_IDS.embeds, view: 'config:appearance' }
    }
  ]

  const credentialEntries = buildCredentialSearchEntries(
    envVarsFetching || envVarsError ? null : envVars,
    {
      settings: t.settings.nav.keysSettings,
      tools: t.settings.nav.keysTools
    },
    { settings: Settings2, tools: Wrench }
  )

  return {
    appearanceEntries,
    configEntries,
    credentialEntries,
    pluginEntries
  }
}
