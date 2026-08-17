import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { describe, test } from 'vitest'

import {
  __testInternals,
  readIntroImageConfig,
  resolveIntroImage,
  writeIntroImageConfig
} from './intro-image'

const { INTRO_IMAGE_CONFIG_FILENAME, introImageConfigPath, mimeTypeForImage } = __testInternals

function makeTmpDir(): string {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-intro-image-test-'))
}

describe('introImageConfigPath', () => {
  test('lives directly under userData', () => {
    const dir = '/tmp/example-userData'
    assert.equal(introImageConfigPath(dir), path.join(dir, INTRO_IMAGE_CONFIG_FILENAME))
  })
})

describe('mimeTypeForImage', () => {
  test('maps known extensions to mime types', () => {
    assert.equal(mimeTypeForImage('/a/b/c.png'), 'image/png')
    assert.equal(mimeTypeForImage('/a/b/c.JPG'), 'image/jpeg')
    assert.equal(mimeTypeForImage('/a/b/c.WebP'), 'image/webp')
    assert.equal(mimeTypeForImage('/a/b/c.svg'), 'image/svg+xml')
  })

  test('rejects unsupported extensions', () => {
    assert.equal(mimeTypeForImage('/a/b/c.txt'), null)
    assert.equal(mimeTypeForImage('/a/b/c.exe'), null)
    assert.equal(mimeTypeForImage('/a/b/c'), null)
  })
})

describe('readIntroImageConfig', () => {
  test('returns null when config file is missing', () => {
    const dir = makeTmpDir()
    assert.deepEqual(readIntroImageConfig(dir), { imagePath: null })
  })

  test('returns the stored path when present', () => {
    const dir = makeTmpDir()
    fs.writeFileSync(
      path.join(dir, INTRO_IMAGE_CONFIG_FILENAME),
      JSON.stringify({ imagePath: '/Users/me/Pictures/hero.png' })
    )
    assert.deepEqual(readIntroImageConfig(dir), { imagePath: '/Users/me/Pictures/hero.png' })
  })

  test('treats whitespace-only paths as null', () => {
    const dir = makeTmpDir()
    fs.writeFileSync(
      path.join(dir, INTRO_IMAGE_CONFIG_FILENAME),
      JSON.stringify({ imagePath: '   ' })
    )
    assert.deepEqual(readIntroImageConfig(dir), { imagePath: null })
  })

  test('returns null on malformed JSON', () => {
    const dir = makeTmpDir()
    fs.writeFileSync(path.join(dir, INTRO_IMAGE_CONFIG_FILENAME), 'not json {{{')
    assert.deepEqual(readIntroImageConfig(dir), { imagePath: null })
  })

  test('returns null on non-string imagePath', () => {
    const dir = makeTmpDir()
    fs.writeFileSync(
      path.join(dir, INTRO_IMAGE_CONFIG_FILENAME),
      JSON.stringify({ imagePath: 42 })
    )
    assert.deepEqual(readIntroImageConfig(dir), { imagePath: null })
  })
})

describe('writeIntroImageConfig', () => {
  test('persists the path to disk', () => {
    const dir = makeTmpDir()
    writeIntroImageConfig(dir, '/Users/me/Pictures/hero.png')

    const raw = fs.readFileSync(path.join(dir, INTRO_IMAGE_CONFIG_FILENAME), 'utf8')
    assert.deepEqual(JSON.parse(raw), { imagePath: '/Users/me/Pictures/hero.png' })
  })

  test('persists null as explicit clear', () => {
    const dir = makeTmpDir()
    writeIntroImageConfig(dir, null)

    const raw = fs.readFileSync(path.join(dir, INTRO_IMAGE_CONFIG_FILENAME), 'utf8')
    assert.deepEqual(JSON.parse(raw), { imagePath: null })
  })
})

describe('resolveIntroImage', () => {
  test('returns null/null when no config', async () => {
    const dir = makeTmpDir()
    const result = await resolveIntroImage(dir)
    assert.deepEqual(result, { imagePath: null, dataUrl: null, error: null })
  })

  test('returns null dataUrl with descriptive error for unsupported extension', async () => {
    const dir = makeTmpDir()
    writeIntroImageConfig(dir, '/tmp/whatever.txt')

    const result = await resolveIntroImage(dir)
    assert.equal(result.imagePath, '/tmp/whatever.txt')
    assert.equal(result.dataUrl, null)
    assert.match(result.error ?? '', /Unsupported image extension/)
  })

  test('returns dataUrl when configured image exists and is readable', async () => {
    const dir = makeTmpDir()
    const imgPath = path.join(dir, 'hero.png')
    // 1x1 transparent PNG (smallest valid)
    const pngBytes = Buffer.from(
      'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=',
      'base64'
    )
    fs.writeFileSync(imgPath, pngBytes)

    writeIntroImageConfig(dir, imgPath)

    const result = await resolveIntroImage(dir)
    assert.equal(result.error, null)
    assert.equal(result.imagePath, imgPath)
    assert.match(result.dataUrl ?? '', /^data:image\/png;base64,/)
  })

  test('returns error when configured image is missing', async () => {
    const dir = makeTmpDir()
    writeIntroImageConfig(dir, path.join(dir, 'does-not-exist.png'))

    const result = await resolveIntroImage(dir)
    assert.equal(result.dataUrl, null)
    assert.match(result.error ?? '', /Intro image/)
  })
})