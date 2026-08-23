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
 * Exact baseline for English strings a locale intentionally leaves untranslated.
 * Exact counts prevent improvements from silently creating regression headroom;
 * update a baseline only when the intentional debt changes, and explain why.
 */
const EXPECTED_UNTRANSLATED: Record<Locale, number> = {
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

function placeholders(value: string): string[] {
  return [...new Set([...value.matchAll(/\{(\w+)\}/g)].map((match) => match[1]))];
}

const NO_OPTIONAL_PLACEHOLDERS = new Set<string>();

// `{s}` is an English-only plural suffix at these exact call sites. Keep the
// exemption key-scoped so a future data placeholder also named `{s}` is checked.
const OPTIONAL_FORMATTING_PLACEHOLDERS: Readonly<Record<string, ReadonlySet<string>>> = {
  "skills.skillCount": new Set(["s"]),
  "skills.resultCount": new Set(["s"]),
  "config.fields": new Set(["s"]),
  "env.keysCount": new Set(["s"]),
  "env.customConfigured": new Set(["s"]),
};

const ENGLISH_KEYS = leafKeys(en);
const ENGLISH_ENTRIES = leafEntries(en);

describe("web locale coverage", () => {
  it("has an English catalogue to measure against", () => {
    expect(ENGLISH_KEYS.size).toBeGreaterThan(0);
  });

  for (const locale of Object.keys(LOCALES) as Locale[]) {
    it(`${locale} matches its untranslated-string baseline of ${EXPECTED_UNTRANSLATED[locale]}`, () => {
      const translations = LOCALES[locale];
      // Measure authored strings, never the object already merged over English.
      const authored = leafKeys(authoredStrings(translations) ?? translations);
      const untranslated = [...ENGLISH_KEYS].filter((key) => !authored.has(key));

      expect(
        untranslated.length,
        `${locale} is missing ${untranslated.length} of ${ENGLISH_KEYS.size} English strings; baseline is ` +
          `${EXPECTED_UNTRANSLATED[locale]}. Translate new gaps, or update the baseline only for intentional debt ` +
          `and explain why. First few: ${untranslated.slice(0, 8).join(", ")}`,
      ).toBe(EXPECTED_UNTRANSLATED[locale]);
    });

    it(`${locale} keeps every data {placeholder} the English string interpolates`, () => {
      const translations = LOCALES[locale];
      const authored = leafEntries(authoredStrings(translations) ?? translations);
      const broken: string[] = [];

      for (const [key, englishValue] of ENGLISH_ENTRIES) {
        const localeValue = authored.get(key);

        if (typeof englishValue !== "string" || typeof localeValue !== "string") {
          continue;
        }

        const optional = OPTIONAL_FORMATTING_PLACEHOLDERS[key] ?? NO_OPTIONAL_PLACEHOLDERS;
        const localePlaceholders = placeholders(localeValue);
        const lost = placeholders(englishValue).filter(
          (token) => !optional.has(token) && !localePlaceholders.includes(token),
        );

        if (lost.length) {
          broken.push(`${key} lost {${lost.join("} {")}}`);
        }
      }

      expect(
        broken,
        `${locale} drops placeholders these strings interpolate, so the value never reaches the user`,
      ).toEqual([]);
    });

    it(`${locale} keeps authored array-valued entries the same length as English`, () => {
      const authored = leafEntries(authoredStrings(LOCALES[locale]) ?? LOCALES[locale]);
      const mismatched: string[] = [];

      for (const [key, englishValue] of ENGLISH_ENTRIES) {
        if (!Array.isArray(englishValue)) {
          continue;
        }

        const localeValue = authored.get(key);

        // Missing keys belong to the coverage baseline above. Shape checks only
        // apply once this locale actually authors the array-valued entry.
        if (localeValue === undefined) {
          continue;
        }

        if (!Array.isArray(localeValue)) {
          mismatched.push(`${key} (en is an array, ${locale} is not)`);
          continue;
        }

        if (englishValue.length !== localeValue.length) {
          mismatched.push(`${key} (en has ${englishValue.length}, ${locale} has ${localeValue.length})`);
        }
      }

      expect(mismatched, `${locale} changes the shape of these array-valued translation entries`).toEqual([]);
    });

    it(`${locale} defines no strings English has dropped`, () => {
      const authored = leafKeys(authoredStrings(LOCALES[locale]) ?? LOCALES[locale]);
      const unused = [...authored].filter((key) => !ENGLISH_KEYS.has(key));

      expect(unused, `${locale} defines keys absent from en.ts — dead strings to delete`).toEqual([]);
    });
  }
});
