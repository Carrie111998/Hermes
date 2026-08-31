import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { getHermesConfigRecord, type HermesConfigRecord, saveHermesConfig } from '@/hermes'

import { getTranslations, loadTranslations } from './catalog'
import { DEFAULT_LOCALE, localeConfigValue, normalizeLocale } from './languages'
import { setRuntimeI18nLocale } from './runtime'
import type { Locale, Translations } from './types'

export { LOCALE_META } from './languages'

export interface I18nConfigClient {
  getConfig: () => Promise<HermesConfigRecord>
  saveConfig: (config: HermesConfigRecord) => Promise<{ ok: boolean }>
}

const defaultConfigClient: I18nConfigClient = {
  getConfig: () => {
    if (typeof window === 'undefined' || !window.hermesDesktop?.api) {
      return Promise.resolve({})
    }

    return getHermesConfigRecord()
  },
  saveConfig: config => {
    if (typeof window === 'undefined' || !window.hermesDesktop?.api) {
      return Promise.resolve({ ok: true })
    }

    return saveHermesConfig(config)
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

export function getConfigDisplayLanguage(config: HermesConfigRecord): unknown {
  return isRecord(config.display) ? config.display.language : undefined
}

export function withConfigDisplayLanguage(config: HermesConfigRecord, locale: Locale): HermesConfigRecord {
  const display = isRecord(config.display) ? config.display : {}

  return {
    ...config,
    display: {
      ...display,
      language: localeConfigValue(locale)
    }
  }
}

function toError(error: unknown): Error {
  return error instanceof Error ? error : new Error(String(error))
}

const RTL_LOCALES = new Set<Locale>(['ar'])

function applyDocumentLocale(locale: Locale) {
  if (typeof document === 'undefined') {
    return
  }

  document.documentElement.lang = locale
  document.documentElement.dir = RTL_LOCALES.has(locale) ? 'rtl' : 'ltr'
}

export interface I18nContextValue {
  configLoadError: Error | null
  isLoadingConfig: boolean
  isSavingLocale: boolean
  locale: Locale
  saveError: Error | null
  setLocale: (next: Locale) => Promise<void>
  t: Translations
}

const I18nContext = createContext<I18nContextValue>({
  configLoadError: null,
  isLoadingConfig: false,
  isSavingLocale: false,
  locale: DEFAULT_LOCALE,
  saveError: null,
  setLocale: async () => {},
  t: getTranslations(DEFAULT_LOCALE)
})

export interface I18nProviderProps {
  children: ReactNode
  configClient?: I18nConfigClient | null
  initialLocale?: unknown
}

export function I18nProvider({ children, configClient = defaultConfigClient, initialLocale }: I18nProviderProps) {
  const [locale, setLocaleState] = useState<Locale>(DEFAULT_LOCALE)
  const [translations, setTranslations] = useState<Translations>(() => getTranslations(DEFAULT_LOCALE))
  const [isLoadingConfig, setIsLoadingConfig] = useState(false)
  const [isSavingLocale, setIsSavingLocale] = useState(false)
  const [configLoadError, setConfigLoadError] = useState<Error | null>(null)
  const [saveError, setSaveError] = useState<Error | null>(null)
  const localeRef = useRef(locale)
  const localeRequestRef = useRef(0)
  const saveQueueRef = useRef<Promise<void>>(Promise.resolve())

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    localeRef.current = locale
    setRuntimeI18nLocale(locale)
    applyDocumentLocale(locale)
  }, [locale])

  // eslint-disable-next-line no-restricted-syntax -- request-generation ref cancels stale async config/catalog loads
  useEffect(() => {
    if (!configClient && normalizeLocale(initialLocale) === DEFAULT_LOCALE) {
      return undefined
    }

    let cancelled = false
    const requestId = localeRequestRef.current + 1

    localeRequestRef.current = requestId

    setIsLoadingConfig(true)
    setConfigLoadError(null)

    const loadInitialLocale = async () => {
      try {
        const next = configClient
          ? normalizeLocale(getConfigDisplayLanguage(await configClient.getConfig()))
          : normalizeLocale(initialLocale)

        const nextTranslations = await loadTranslations(next)

        if (!cancelled && localeRequestRef.current === requestId) {
          setTranslations(nextTranslations)
          setLocaleState(next)
        }
      } catch (error) {
        if (!cancelled && localeRequestRef.current === requestId) {
          setConfigLoadError(toError(error))
          setTranslations(getTranslations(DEFAULT_LOCALE))
          setLocaleState(DEFAULT_LOCALE)
        }
      } finally {
        if (!cancelled && localeRequestRef.current === requestId) {
          setIsLoadingConfig(false)
        }
      }
    }

    void loadInitialLocale()

    return () => {
      cancelled = true
    }
  }, [configClient, initialLocale])

  const setLocale = useCallback(
    async (next: Locale) => {
      const requestId = localeRequestRef.current + 1
      const previousLocale = localeRef.current
      const previousTranslations = getTranslations(previousLocale)

      localeRequestRef.current = requestId
      setSaveError(null)
      setIsLoadingConfig(false)
      setIsSavingLocale(true)

      try {
        const nextTranslations = await loadTranslations(next)

        if (localeRequestRef.current !== requestId) {
          return
        }

        setTranslations(nextTranslations)
        setLocaleState(next)

        if (configClient) {
          const saveOperation = saveQueueRef.current.then(async () => {
            const latestConfig = await configClient.getConfig()

            if (localeRequestRef.current !== requestId) {
              return
            }

            const result = await configClient.saveConfig(withConfigDisplayLanguage(latestConfig, next))

            if (localeRequestRef.current === requestId && !result.ok) {
              throw new Error('Failed to save language')
            }
          })

          saveQueueRef.current = saveOperation.catch(() => undefined)
          await saveOperation
        }
      } catch (error) {
        if (localeRequestRef.current !== requestId) {
          return
        }

        const nextError = toError(error)

        setTranslations(previousTranslations)
        setLocaleState(previousLocale)
        setSaveError(nextError)

        throw nextError
      } finally {
        if (localeRequestRef.current === requestId) {
          setIsSavingLocale(false)
        }
      }
    },
    [configClient]
  )

  const value = useMemo<I18nContextValue>(
    () => ({
      configLoadError,
      isLoadingConfig,
      isSavingLocale,
      locale,
      saveError,
      setLocale,
      t: translations
    }),
    [configLoadError, isLoadingConfig, isSavingLocale, locale, saveError, setLocale, translations]
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  return useContext(I18nContext)
}
