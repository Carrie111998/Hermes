import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { getHermesConfigRecord, type HermesConfigRecord, saveHermesConfig } from '@/hermes'

import { DEFAULT_TRANSLATIONS, loadTranslations, translationsFor } from './catalog'
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
  t: DEFAULT_TRANSLATIONS
})

export interface I18nProviderProps {
  children: ReactNode
  configClient?: I18nConfigClient | null
  initialLocale?: unknown
}

export function I18nProvider({ children, configClient = defaultConfigClient, initialLocale }: I18nProviderProps) {
  const [locale, setLocaleState] = useState<Locale>(() => normalizeLocale(initialLocale))
  const [isLoadingConfig, setIsLoadingConfig] = useState(false)
  const [isSavingLocale, setIsSavingLocale] = useState(false)
  const [configLoadError, setConfigLoadError] = useState<Error | null>(null)
  const [saveError, setSaveError] = useState<Error | null>(null)
  const localeRef = useRef(locale)

  // Non-English message trees are separate chunks (see catalog.ts), so the
  // tree for a locale can arrive an import after the locale itself is chosen.
  // Locale and messages are therefore held TOGETHER: `dir`/`lang` and the copy
  // on screen must never disagree.
  //
  // Splitting them is a real bug in RTL, not a cosmetic one. `ar` sets
  // `dir="rtl"`; if direction followed `locale` while the copy waited on the
  // chunk, the whole layout would mirror around English text and then swap
  // again — a visible flip, not the invisible late-text swap LTR locales get.
  //
  // `applied` therefore only advances once a tree is in hand. Until then the
  // previous locale keeps rendering, so a switch reads as one transition
  // rather than a bounce through English.
  // An already-loaded tree resolves DURING render, not in an effect. English
  // is always loaded, so falling back to it (a failed config read, an
  // unsupported `display.language`) stays a single synchronous commit — going
  // through state there would leave one frame of the previous language on
  // screen after the app had already decided on English.
  //
  // State only carries trees that arrive asynchronously; once `loadTranslations`
  // caches one, `translationsFor` sees it on the very next render and this
  // fallback stops being consulted.
  const [asyncLoaded, setAsyncLoaded] = useState<{ locale: Locale; messages: Translations }>(() => ({
    locale: DEFAULT_LOCALE,
    messages: DEFAULT_TRANSLATIONS
  }))

  const readyMessages = translationsFor(locale)
  const applied = readyMessages ? { locale, messages: readyMessages } : asyncLoaded

  useEffect(() => {
    if (translationsFor(locale)) {
      return
    }

    let cancelled = false

    void loadTranslations(locale).then(loadedMessages => {
      if (!cancelled && loadedMessages) {
        setAsyncLoaded({ locale, messages: loadedMessages })
      }
    })

    return () => {
      cancelled = true
    }
  }, [locale])

  // Document direction follows `applied`, not `locale`, for the reason above.
  // `runtimeLocale` too: translateNow resolves against the loaded catalog, so
  // pointing it at a locale whose chunk has not landed would just return
  // English while the surrounding UI already claimed that locale.
  useEffect(() => {
    setRuntimeI18nLocale(applied.locale)
    applyDocumentLocale(applied.locale)
  }, [applied.locale])

  // The rollback target in `setLocale` is the user's SELECTION, so this ref
  // tracks `locale` — not `applied.locale`, which may still be a tick behind.
  // eslint-disable-next-line no-restricted-syntax -- legitimate non-atom ref write (see eslint rule comment)
  useEffect(() => {
    localeRef.current = locale
  }, [locale])

  useEffect(() => {
    if (!configClient) {
      return
    }

    let cancelled = false

    setIsLoadingConfig(true)
    setConfigLoadError(null)

    configClient
      .getConfig()
      .then(config => {
        if (!cancelled) {
          setLocaleState(normalizeLocale(getConfigDisplayLanguage(config)))
        }
      })
      .catch(error => {
        if (!cancelled) {
          setConfigLoadError(toError(error))
          setLocaleState(DEFAULT_LOCALE)
        }
      })
      .finally(() => {
        if (!cancelled) {
          setIsLoadingConfig(false)
        }
      })

    return () => {
      cancelled = true
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
      t: applied.messages
    }),
    [applied.messages, configLoadError, isLoadingConfig, isSavingLocale, locale, saveError, setLocale]
  )

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>
}

export function useI18n(): I18nContextValue {
  return useContext(I18nContext)
}
