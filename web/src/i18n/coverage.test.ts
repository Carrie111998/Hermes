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
 * its ceiling afterwards keeps the ratchet tight. *
 * A ceiling is deliberately a number and not a hard 100% gate. If a locale wants
 * to defer a feature's strings, raise its ceiling in the same commit and say why
 * — an honest gap is worth more than English text pasted in to go green, which
 * looks translated to every tool and to the user until someone reports it.
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

function leafEntries(value: unknown, prefix = "", out = new Map<string, unknown>()): Map<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    if (prefix) {
      out.set(prefix, value);
    }

    return out;
  }

  for (const [key, child] of Object.entries(value)) {
    leafEntries(child, prefix ? `${prefix}.${key}` : key, out);
  }

  return out;
}

/**
 * Data placeholders in a string. `{s}` is excluded: call sites replace it with a
 * literal English "s" for pluralization, so it is a formatting marker rather
 * than a value, and a locale is right to omit it.
 */
function placeholders(value: string): string[] {
  return [...new Set([...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]))].filter((token) => token !== "s");
}

const ENGLISH_KEYS = leafKeys(en);
const ENGLISH_ENTRIES = leafEntries(en);

describe("web locale coverage", () => {
  it("has an English catalogue to measure against", () => {
    expect(ENGLISH_KEYS.size).toBeGreaterThan(0);
  });

  for (const locale of Object.keys(LOCALES) as Locale[]) {
    it(`${locale} leaves at most ${MAX_UNTRANSLATED[locale]} strings untranslated`, () => {
      const translations = LOCALES[locale];
      // The invariant this file rests on: measure what the locale **authored**,
      // never what it renders. `defineLocale` has already merged `ar` over `en`,
      // so measuring the merged object reports full coverage for every locale
      // and catches nothing. `authoredStrings` returns the pre-merge overrides;
      // the fallback is for a `Translations` literal, which is its own answer.
      const authored = leafKeys(authoredStrings(translations) ?? translations);
      const untranslated = [...ENGLISH_KEYS].filter((key) => !authored.has(key));

      expect(
        untranslated.length,
        `${locale} is missing ${untranslated.length} of ${ENGLISH_KEYS.size} English strings, which will render in English. ` +
          `Translate them in ${locale}.ts. First few: ${untranslated.slice(0, 8).join(", ")}`,
      ).toBeLessThanOrEqual(MAX_UNTRANSLATED[locale]);
    });

    // Key presence is not enough. A translation that keeps the key but drops a
    // `{placeholder}` renders the literal token to the user, and counting keys
    // cannot see it. A hard assertion, not a ceiling: every locale passes today.
    it(`${locale} keeps every {placeholder} the English string interpolates`, () => {
      const translations = LOCALES[locale];
      const authored = leafEntries(authoredStrings(translations) ?? translations);
      const broken: string[] = [];

      for (const [key, englishValue] of ENGLISH_ENTRIES) {
        const localeValue = authored.get(key);

        if (typeof englishValue !== "string" || typeof localeValue !== "string") {
          continue;
        }

        const lost = placeholders(englishValue).filter((token) => !placeholders(localeValue).includes(token));

        if (lost.length) {
          broken.push(`${key} lost {${lost.join("} {")}}`);
        }
      }

      expect(
        broken,
        `${locale} drops placeholders these strings interpolate, so the value never reaches the user`,
      ).toEqual([]);
    });

    it(`${locale} defines no strings English has dropped`, () => {
      const authored = leafKeys(authoredStrings(LOCALES[locale]) ?? LOCALES[locale]);
      const unused = [...authored].filter((key) => !ENGLISH_KEYS.has(key));

      expect(unused, `${locale} defines keys absent from en.ts — dead strings to delete`).toEqual([]);
    });
  }
});
