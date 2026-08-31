/**
 * Locale catalog — English static, everything else on demand.
 *
 * The five message trees total ~908 KB of source and built into a single
 * 699 KB chunk that `index.html` modulepreloaded on every launch. A user
 * reads one language, so four fifths of that was always dead weight on the
 * cold boot path.
 *
 * English stays static because it is both `DEFAULT_LOCALE` and the fallback
 * arm of `translateFrom` — every miss in every other locale resolves through
 * it, so it is genuinely needed before first paint. The other four are
 * dynamic imports, fetched when a locale actually becomes active.
 *
 * This does not introduce a new flash of English. `I18nProvider` already
 * starts at `DEFAULT_LOCALE` and switches only after its async
 * `configClient.getConfig()` resolves — the locale chunk is fetched on that
 * same transition, so the swap the user sees is the one that already existed.
 *
 * Sync reads that arrive before the chunk lands (`translationsFor`) return
 * `undefined`, which `translateFrom` already handles by falling back to
 * English. That fallback is load-bearing here, not incidental — do not
 * "simplify" it away.
 */

import { en } from './en'
import type { Locale, Translations } from './types'

type LocaleLoader = () => Promise<{ default?: Translations } & Record<string, unknown>>

// Each entry must be a literal dynamic `import()` so the bundler can see it
// and emit one chunk per locale. A computed specifier (`import('./' + id)`)
// defeats that and pulls the whole directory back into one chunk.
const LOADERS: Record<Exclude<Locale, 'en'>, LocaleLoader> = {
  ar: () => import('./ar'),
  ja: () => import('./ja'),
  zh: () => import('./zh'),
  'zh-hant': () => import('./zh-hant')
}

// Named export per module (`export const ar = …`), so pick the entry whose
// key matches rather than assuming a default export.
const EXPORT_NAMES: Record<Exclude<Locale, 'en'>, string> = {
  ar: 'ar',
  ja: 'ja',
  zh: 'zh',
  'zh-hant': 'zhHant'
}

const loaded: Partial<Record<Locale, Translations>> = { en }
const inFlight = new Map<Locale, Promise<Translations | undefined>>()

/** The message tree for `locale`, or `undefined` if it has not loaded yet. */
export function translationsFor(locale: Locale): Translations | undefined {
  return loaded[locale]
}

/** English — always present, and the fallback every other locale resolves through. */
export const DEFAULT_TRANSLATIONS: Translations = en

/**
 * Fetch `locale`'s message tree, caching both the result and the in-flight
 * promise so concurrent callers (provider effect + a runtime translate) share
 * one request. Resolves to `undefined` if the chunk fails to load — callers
 * keep rendering English rather than crashing the shell over a missing
 * translation bundle.
 */
export function loadTranslations(locale: Locale): Promise<Translations | undefined> {
  const cached = loaded[locale]

  if (cached) {
    return Promise.resolve(cached)
  }

  const pending = inFlight.get(locale)

  if (pending) {
    return pending
  }

  const loader = LOADERS[locale as Exclude<Locale, 'en'>]

  if (!loader) {
    return Promise.resolve(undefined)
  }

  const promise = loader()
    .then(module => {
      const messages = module[EXPORT_NAMES[locale as Exclude<Locale, 'en'>]] as Translations | undefined

      if (messages) {
        loaded[locale] = messages
      }

      return messages
    })
    .catch(() => undefined)
    .finally(() => {
      inFlight.delete(locale)
    })

  inFlight.set(locale, promise)

  return promise
}
