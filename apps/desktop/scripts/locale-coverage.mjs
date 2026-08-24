#!/usr/bin/env node
// Reports how much of en.ts each locale actually translates.
//
// `defineLocale()` merges every locale onto the English base, so a locale
// object always has 100% of the keys at runtime and coverage looks perfect
// even when nothing was translated. What matters is how many leaves still
// hold the English value. That is what this reports.
//
// Run it through tsx, since it imports the TypeScript catalog directly:
//
//   npm run locale:coverage                  # table
//   npm run locale:coverage -- --json        # machine readable
//   npm run locale:coverage -- --list de     # the untranslated keys for one locale
import { pathToFileURL } from 'node:url'

function leaves(node, prefix = '', out = new Map()) {
  if (node === null || typeof node !== 'object') return out
  for (const [key, value] of Object.entries(node)) {
    const path = `${prefix}${key}`
    if (value !== null && typeof value === 'object' && !Array.isArray(value)) {
      leaves(value, `${path}.`, out)
    } else {
      out.set(path, typeof value === 'function' ? String(value) : JSON.stringify(value))
    }
  }
  return out
}

const catalog = await import(pathToFileURL(new URL('../src/i18n/catalog.ts', import.meta.url).pathname))
const translations = catalog.TRANSLATIONS
const english = leaves(translations.en)

// Derived from the catalog, so a new locale is measured without touching this file.
const locales = Object.keys(translations).filter(id => id !== 'en')

const report = locales.map(id => {
  const own = leaves(translations[id])
  const untranslated = [...english].filter(([path, value]) => own.get(path) === value).map(([path]) => path)
  return {
    locale: id,
    keys: english.size,
    untranslated: untranslated.length,
    coverage: Number((((english.size - untranslated.length) / english.size) * 100).toFixed(1)),
    paths: untranslated
  }
})

const listIndex = process.argv.indexOf('--list')

if (listIndex !== -1) {
  const wanted = process.argv[listIndex + 1]
  const row = report.find(r => r.locale === wanted)
  if (!row) {
    console.error(`unknown locale: ${wanted}`)
    process.exit(1)
  }
  console.log(row.paths.join('\n'))
} else if (process.argv.includes('--json')) {
  const summary = report.map(row => ({ locale: row.locale, keys: row.keys, untranslated: row.untranslated, coverage: row.coverage }))
  console.log(JSON.stringify(summary, null, 2))
} else {
  console.log(`en.ts leaf keys: ${english.size}\n`)
  console.log('locale     untranslated   coverage')
  for (const row of report) {
    console.log(`${row.locale.padEnd(10)} ${String(row.untranslated).padStart(12)}   ${String(row.coverage).padStart(6)}%`)
  }
}
