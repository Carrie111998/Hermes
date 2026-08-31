import { configure } from '@testing-library/react'

// Node 26 defines its own `localStorage` accessor on the global object, which
// returns `undefined` unless the process was started with --localstorage-file
// (it warns: "localStorage is not available because --localstorage-file was
// not provided"). In the jsdom environment `globalThis` IS the window, so that
// accessor shadows jsdom's Storage and every `localStorage.getItem(...)` in a
// test throws "Cannot read properties of undefined". Install a real in-memory
// Storage when the global resolves to nothing, before any test module reads it.
if (typeof (globalThis as any).localStorage === 'undefined') {
  const store = new Map<string, string>()
  const storage: Storage = {
    get length() {
      return store.size
    },
    key: (i: number) => [...store.keys()][i] ?? null,
    getItem: (k: string) => store.get(String(k)) ?? null,
    setItem: (k: string, v: string) => void store.set(String(k), String(v)),
    removeItem: (k: string) => void store.delete(String(k)),
    clear: () => store.clear(),
  }
  for (const target of [globalThis, (globalThis as any).window].filter(Boolean)) {
    Object.defineProperty(target, 'localStorage', {
      value: storage,
      configurable: true,
      writable: true,
    })
  }
}

// React 19 + Testing Library 16: opt into the act environment so render(),
// fireEvent(), and findBy* queries automatically flush state updates without
// spurious "not wrapped in act(...)" warnings.
;(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true

// findBy*/waitFor default to a 1000ms deadline — too tight for async-heavy
// panels (radix menus, refetch chains) when the full suite runs under xdist
// CPU contention in CI. Success still resolves the instant the node appears;
// the wider deadline only absorbs a starved runner, killing timing flakes.
configure({ asyncUtilTimeout: 5000 })

// Number and date formatting in the app deliberately follows the host locale
// (`Intl.*` and `toLocaleString()` called with no locale — see src/lib/time.ts).
// Tests assert en-US output, which holds silently on CI's en-US runners and
// breaks for any contributor whose machine is set to another locale: tr-TR
// renders 1.234.567, de-DE renders 1.234.567, fr-FR renders 1 234 567. An env
// var cannot fix this portably — ICU reads the locale from the OS on Windows
// and ignores LANG/LC_ALL — so pin the default the tests were written against.
const TEST_LOCALE = 'en-US'

type LocaleFormatter = (this: unknown, locales?: unknown, options?: unknown) => string

const withDefaultLocale = <T extends LocaleFormatter>(format: T): T =>
  function (this: unknown, locales?: unknown, options?: unknown) {
    return format.call(this, locales ?? TEST_LOCALE, options)
  } as T

Number.prototype.toLocaleString = withDefaultLocale(Number.prototype.toLocaleString)
BigInt.prototype.toLocaleString = withDefaultLocale(BigInt.prototype.toLocaleString)
Date.prototype.toLocaleString = withDefaultLocale(Date.prototype.toLocaleString)
Date.prototype.toLocaleDateString = withDefaultLocale(Date.prototype.toLocaleDateString)
Date.prototype.toLocaleTimeString = withDefaultLocale(Date.prototype.toLocaleTimeString)

// `new Intl.NumberFormat(undefined, …)` resolves to the host locale too, so the
// constructors need the same default. A Proxy keeps `instanceof` and the static
// `supportedLocalesOf` intact, unlike a hand-rolled subclass.
for (const name of ['NumberFormat', 'DateTimeFormat', 'RelativeTimeFormat', 'ListFormat', 'PluralRules'] as const) {
  const Original = Intl[name] as unknown as new (locales?: unknown, options?: unknown) => unknown

  ;(Intl as unknown as Record<string, unknown>)[name] = new Proxy(Original, {
    construct: (target, [locales, options]) => new target(locales ?? TEST_LOCALE, options),
    apply: (target, thisArg, [locales, options]) =>
      Reflect.apply(target as never, thisArg, [locales ?? TEST_LOCALE, options])
  })
}
