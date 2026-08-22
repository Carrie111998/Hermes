import { describe, expect, it } from "vitest";

import { af } from "./af";
import { ar } from "./ar";
import { authoredStrings } from "./define-locale";
import { de } from "./de";
import { en } from "./en";
import { es } from "./es";
import { fr } from "./fr";
import { ga } from "./ga";
import { hu } from "./hu";
import { it as itLocale } from "./it";
import { ja } from "./ja";
import { ko } from "./ko";
import { pt } from "./pt";
import { ru } from "./ru";
import { tr } from "./tr";
import type { Locale, Translations } from "./types";
import { uk } from "./uk";
import { zh } from "./zh";
import { zhHant } from "./zh-hant";

// Typed as Record<Locale, …> on purpose: a new locale fails to compile here
// until it is listed, so it cannot slip past this file unmeasured.
const LOCALES: Record<Locale, Translations> = {
  en,
  zh,
  "zh-hant": zhHant,
  ja,
  de,
  es,
  fr,
  tr,
  uk,
  af,
  ko,
  it: itLocale,
  ga,
  pt,
  ru,
  hu,
  ar,
};

/**
 * Ceiling on the English strings each locale may leave untranslated.
 *
 * Untranslated strings are invisible here by construction. Most of
 * `Translations` is optional (`?:`) so a locale can omit a key and still
 * type-check, and `ar` goes through `defineLocale`, which merges it over `en`
 * before anyone can look — so at runtime an untranslated key is
 * indistinguishable from a translated one. Nothing surfaces the gap.
 *
 * The numbers only ever go down. Translating more of a locale is free; lowering
 * its ceiling afterwards keeps the ratchet tight.
 */
const MAX_UNTRANSLATED: Record<Locale, number> = {
  en: 0,
  ar: 0,
  af: 79,
  de: 79,
  es: 79,
  fr: 79,
  ga: 79,
  hu: 79,
  it: 79,
  ja: 79,
  ko: 79,
  pt: 79,
  ru: 79,
  tr: 79,
  uk: 79,
  zh: 79,
  "zh-hant": 79,
};

function leafKeys(value: unknown, prefix = "", out = new Set<string>()): Set<string> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    if (prefix) {
      out.add(prefix);
    }

    return out;
  }

  for (const [key, child] of Object.entries(value)) {
    leafKeys(child, prefix ? `${prefix}.${key}` : key, out);
  }

  return out;
}

const ENGLISH_KEYS = leafKeys(en);

describe("web locale coverage", () => {
  it("has an English catalogue to measure against", () => {
    expect(ENGLISH_KEYS.size).toBeGreaterThan(0);
  });

  for (const locale of Object.keys(LOCALES) as Locale[]) {
    it(`${locale} leaves at most ${MAX_UNTRANSLATED[locale]} strings untranslated`, () => {
      const translations = LOCALES[locale];
      // What the locale wrote, not what it renders: the override object for a
      // partial locale, the locale itself for a `Translations` literal.
      const authored = leafKeys(authoredStrings(translations) ?? translations);
      const untranslated = [...ENGLISH_KEYS].filter((key) => !authored.has(key));

      expect(
        untranslated.length,
        `${locale} is missing ${untranslated.length} of ${ENGLISH_KEYS.size} English strings, which will render in English. ` +
          `Translate them in ${locale}.ts. First few: ${untranslated.slice(0, 8).join(", ")}`,
      ).toBeLessThanOrEqual(MAX_UNTRANSLATED[locale]);
    });

    it(`${locale} defines no strings English has dropped`, () => {
      const authored = leafKeys(authoredStrings(LOCALES[locale]) ?? LOCALES[locale]);
      const unused = [...authored].filter((key) => !ENGLISH_KEYS.has(key));

      expect(unused, `${locale} defines keys absent from en.ts — dead strings to delete`).toEqual([]);
    });
  }
});
