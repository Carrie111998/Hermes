import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import {
  foundrlySoulMd,
  provisionDesktopBrand,
  shouldWriteBrandSoul,
  soulMd
} from './desktop-brand-provision'

test('Foundrly rich soul names co-founder product and not IX Agency', () => {
  const soul = foundrlySoulMd({
    webUrl: 'https://getfoundrly.com',
    adminPortalUrl: 'https://admin.intelli-verse-x.ai/admin/portal'
  })

  assert.match(soul, /You are Foundrly/)
  assert.match(soul, /AI co-founder/)
  assert.match(soul, /foundrly\.intelli-verse-x\.ai/)
  assert.match(soul, /NOT IX Agency/)
  assert.doesNotMatch(soul, /^You are IX Agency/m)
})

test('shouldWriteBrandSoul upgrades IX Agency and thin Foundrly stock only', () => {
  assert.equal(shouldWriteBrandSoul(null, 'foundrly', 'Foundrly'), true)
  assert.equal(shouldWriteBrandSoul('', 'foundrly', 'Foundrly'), true)
  assert.equal(
    shouldWriteBrandSoul('You are IX Agency, an intelligent AI assistant created by Nous Research.', 'foundrly', 'Foundrly'),
    true
  )
  assert.equal(shouldWriteBrandSoul(soulMd('Foundrly'), 'foundrly', 'Foundrly'), true)
  assert.equal(shouldWriteBrandSoul(foundrlySoulMd(), 'foundrly', 'Foundrly'), false)
  assert.equal(
    shouldWriteBrandSoul('# Custom\n\nYou are a pirate assistant for my bakery.', 'foundrly', 'Foundrly'),
    false
  )
  assert.equal(shouldWriteBrandSoul(soulMd('Agency'), 'ix-agency', 'Agency'), true)
  assert.equal(
    shouldWriteBrandSoul('You are IX Agency, an intelligent AI assistant.', 'ix-agency', 'IX Agency'),
    false
  )
})

test('provisionDesktopBrand writes rich Foundrly soul into an isolated home', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'foundrly-soul-'))

  try {
    const result = provisionDesktopBrand({
      brandId: 'foundrly',
      hermesHome: home,
      productName: 'Foundrly',
      foundrly: {
        webUrl: 'https://getfoundrly.com',
        adminPortalUrl: 'https://admin.intelli-verse-x.ai/admin/portal',
        adminChatUrl: 'https://admin.intelli-verse-x.ai/admin/portal/chat'
      }
    })

    assert.equal(result.soulWritten, true)
    const soul = fs.readFileSync(result.soulPath, 'utf8')
    assert.match(soul, /AI co-founder desktop/)
    assert.match(soul, /foundrly\.intelli-verse-x\.ai/)
  } finally {
    fs.rmSync(home, { force: true, recursive: true })
  }
})

test('provisionDesktopBrand upgrades leftover IX Agency soul for Foundrly', () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'foundrly-upgrade-'))

  try {
    fs.writeFileSync(
      path.join(home, 'SOUL.md'),
      'You are IX Agency, an intelligent AI assistant created by Nous Research.\n',
      'utf8'
    )

    const result = provisionDesktopBrand({
      brandId: 'foundrly',
      hermesHome: home,
      productName: 'Foundrly'
    })

    assert.equal(result.soulWritten, true)
    assert.match(fs.readFileSync(path.join(home, 'SOUL.md'), 'utf8'), /You are Foundrly/)
  } finally {
    fs.rmSync(home, { force: true, recursive: true })
  }
})
