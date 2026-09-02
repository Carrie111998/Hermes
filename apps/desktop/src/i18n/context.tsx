import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { getHermesConfigRecord, type HermesConfigRecord, saveHermesConfig } from '@/hermes'

import { TRANSLATIONS } from './catalog'
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
  t: TRANSLATIONS[DEFAULT_LOCALE]
})

export interface I18nProviderProps {
  children: ReactNode
  configClient?: I18nConfigClient | null
  initialLocale?: unknown
}

/** One-shot `/api/config` can miss a Windows cold-start GIL stall. Four
 *  attempts with exponential backoff (1s, 2s, 4s) cover the 12–28s import
 *  window without locking English for the rest of the session (#96177). */
export const CONFIG_LOAD_MAX_ATTEMPTS = 4
export const CONFIG_LOAD_BASE_DELAY_MS = 1_000

function configLoadRetryDelayMs(attempt: number): number {
  return CONFIG_LOAD_BASE_DELAY_MS * 2 ** attempt
}

export function I18nProvider({ children, configClient = defaultConfigClient, initialLocale }: I18nProviderProps) {
  const [locale, setLocaleState] = useState<Locale>(() => normalizeLocale(initialLocale))
  const [isLoadingConfig, setIsLoadingConfig] = useState(false)
  const [isSavingLocale, setIsSavingLocale] = useState(false)
  const [configLoadError, setConfigLoadError] = useState<Error | null>(null)
  const [saveError, setSaveError] = useState<Error | null>(null)
  const localeRef = useRef(locale)

  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    localeRef.current = locale
    setRuntimeI18nLocale(locale)
    applyDocumentLocale(locale)
  }, [locale])

  useEffect(() => {
    if (!configClient) {
      return
    }

    let cancelled = false
    let retryTimeoutId: ReturnType<typeof setTimeout> | undefined

    const finishFailure = (error: unknown) => {
      setConfigLoadError(toError(error))
      setLocaleState(DEFAULT_LOCALE)
      setIsLoadingConfig(false)
    }

    const fetchConfig = (attempt: number) => {
      if (cancelled) {
        return
      }

      configClient
        .getConfig()
        .then(config => {
          if (cancelled) {
            return
          }

          const language = getConfigDisplayLanguage(config)

          if (language) {
            setLocaleState(normalizeLocale(language))
            setConfigLoadError(null)
            setIsLoadingConfig(false)
            return
          }

          // Gateway answered but display.language is not in the payload yet
          // (still warming). Retry rather than snapping to English.
          if (attempt + 1 < CONFIG_LOAD_MAX_ATTEMPTS) {
            retryTimeoutId = setTimeout(() => fetchConfig(attempt + 1), configLoadRetryDelayMs(attempt))
            return
          }

          setLocaleState(normalizeLocale(language))
          setIsLoadingConfig(false)
        })
        .catch(error => {
          if (cancelled) {
            return
          }

          if (attempt + 1 < CONFIG_LOAD_MAX_ATTEMPTS) {
            retryTimeoutId = setTimeout(() => fetchConfig(attempt + 1), configLoadRetryDelayMs(attempt))
            return
          }

          finishFailure(error)
        })
    }

    setIsLoadingConfig(true)
    setConfigLoadError(null)
    fetchConfig(0)

    return () => {
      cancelled = true

      if (retryTimeoutId !== undefined) {
        clearTimeout(retryTimeoutId)
      }
    }
  }, [configClient, initialLocale])

  const setLocale = useCallback(
    async (next: Locale) => {
      const previousLocale = localeRef.current

      setSaveError(null)
      setLocaleState(next)

      if (!configClient) {
        return
      }

      setIsSavingLocale(true)

      try {
        const latestConfig = await configClient.getConfig()
        const result = await configClient.saveConfig(withConfigDisplayLanguage(latestConfig, next))

        if (!result.ok) {
          throw new Error('Failed to save language')
        }
      } catch (error) {
        const nextError = toError(error)

        setLocaleState(previousLocale)
        setSaveError(nextError)

        throw nextError
      } finally {
        setIsSavingLocale(false)
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
      t: TRANSLATIONS[locale]
    }),
    [configLoadError, isLoadingConfig, isSavingLocale, locale, saveError, setLocale]
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  return useContext(I18nContext)
}
