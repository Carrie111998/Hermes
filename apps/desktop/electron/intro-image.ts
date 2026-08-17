// Custom intro image for the desktop chat welcome screen.
//
// Stored as JSON in Electron's userData dir (same convention as
// `project-dir.json`) so it survives self-updates without bleeding into the
// new install. `imagePath === null` means "no preference, render the default
// wordmark." The renderer fetches via `introImage.get()`, which returns both
// the raw path and a `data:` URL (or null when the file is missing /
// unreadable / wrong mime).

import fs from 'node:fs'
import path from 'node:path'

import { readFileDataUrlForIpc } from './hardening'

const INTRO_IMAGE_CONFIG_FILENAME = 'intro-image.json'

// Keep intro images small — they're inlined as data URLs in the renderer.
const INTRO_IMAGE_MAX_BYTES = 5 * 1024 * 1024

const INTRO_IMAGE_MIME_TYPES: Record<string, string> = {
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.svg': 'image/svg+xml'
}

const INTRO_IMAGE_EXTENSIONS = new Set(Object.keys(INTRO_IMAGE_MIME_TYPES))

export interface IntroImageConfig {
  imagePath: string | null
}

export interface IntroImageResolution {
  imagePath: string | null
  dataUrl: string | null
  error: string | null
}

function introImageConfigPath(userDataDir: string): string {
  return path.join(userDataDir, INTRO_IMAGE_CONFIG_FILENAME)
}

export function readIntroImageConfig(userDataDir: string): IntroImageConfig {
  const target = introImageConfigPath(userDataDir)

  try {
    const raw = fs.readFileSync(target, 'utf8')
    const parsed = JSON.parse(raw) as { imagePath?: unknown }

    if (parsed && typeof parsed.imagePath === 'string' && parsed.imagePath.trim()) {
      return { imagePath: parsed.imagePath.trim() }
    }

    if (parsed && parsed.imagePath === null) {
      return { imagePath: null }
    }
  } catch {
    // Missing / unreadable / malformed → no preference.
  }

  return { imagePath: null }
}

export function writeIntroImageConfig(userDataDir: string, imagePath: string | null): IntroImageConfig {
  const target = introImageConfigPath(userDataDir)
  const payload = JSON.stringify({ imagePath: imagePath ?? null }, null, 2)

  try {
    fs.mkdirSync(userDataDir, { recursive: true })
    fs.writeFileSync(target, payload, 'utf8')
  } catch {
    // Best-effort persistence — the caller's already received the value back.
  }

  return { imagePath: imagePath ?? null }
}

function mimeTypeForImage(imagePath: string): string | null {
  const ext = path.extname(imagePath).toLowerCase()

  if (!INTRO_IMAGE_EXTENSIONS.has(ext)) {
    return null
  }

  return INTRO_IMAGE_MIME_TYPES[ext] ?? null
}

export async function resolveIntroImage(userDataDir: string): Promise<IntroImageResolution> {
  const { imagePath } = readIntroImageConfig(userDataDir)

  if (!imagePath) {
    return { imagePath: null, dataUrl: null, error: null }
  }

  const mimeType = mimeTypeForImage(imagePath)

  if (!mimeType) {
    return {
      imagePath,
      dataUrl: null,
      error: `Unsupported image extension. Use one of: ${[...INTRO_IMAGE_EXTENSIONS].join(', ')}.`
    }
  }

  try {
    const dataUrl = await readFileDataUrlForIpc(imagePath, {
      purpose: 'Intro image',
      mimeType,
      maxBytes: INTRO_IMAGE_MAX_BYTES
    })

    return { imagePath, dataUrl, error: null }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)

    return { imagePath, dataUrl: null, error: message }
  }
}

export interface IntroImageModule {
  resolveIntroImage: (userDataDir: string) => Promise<IntroImageResolution>
  readIntroImageConfig: (userDataDir: string) => IntroImageConfig
  writeIntroImageConfig: (userDataDir: string, imagePath: string | null) => IntroImageConfig
}

// eslint-disable-next-line camelcase
export const __testInternals = {
  INTRO_IMAGE_CONFIG_FILENAME,
  INTRO_IMAGE_MAX_BYTES,
  INTRO_IMAGE_MIME_TYPES,
  INTRO_IMAGE_EXTENSIONS,
  introImageConfigPath,
  mimeTypeForImage
}