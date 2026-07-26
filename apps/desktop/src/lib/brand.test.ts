import { describe, expect, it } from 'vitest'

import foundrlyBrand from '../../brands/foundrly.json'
import ixAgencyBrand from '../../brands/ix-agency.json'
import quizverseBrand from '../../brands/quizverse.json'

import {
  BRAND,
  DESKTOP_BRAND_ID,
  IS_FOUNDRLY_BRAND,
  IS_IX_AGENCY_BRAND,
  IS_QUIZVERSE_BRAND
} from './brand'

const MANIFESTS = [ixAgencyBrand, quizverseBrand, foundrlyBrand]

const IDENTITY_FIELDS = [
  'appId',
  'artifactPrefix',
  'executableName',
  'productName',
  'protocolScheme',
  's3PublishPath',
  'updateFeedUrl'
] as const

describe('brand manifests', () => {
  it('every manifest carries a complete identity', () => {
    for (const manifest of MANIFESTS) {
      expect(manifest.id).toBeTruthy()

      for (const field of IDENTITY_FIELDS) {
        expect(manifest[field], `${manifest.id}.${field}`).toBeTruthy()
      }
    }
  })

  it('no identity value is shared between brands — separate userData, feeds, links', () => {
    for (const field of IDENTITY_FIELDS) {
      const values = MANIFESTS.map(manifest => manifest[field])

      expect(new Set(values).size, field).toBe(MANIFESTS.length)
    }
  })

  it('each brand publishes to its own S3 prefix and polls its own feed', () => {
    for (const manifest of MANIFESTS) {
      expect(manifest.updateFeedUrl.endsWith(`/${manifest.s3PublishPath}`), manifest.id).toBe(true)
    }
  })

  it('no brand borrows another brand’s download landing page', () => {
    for (const manifest of MANIFESTS) {
      const others = MANIFESTS.filter(other => other.id !== manifest.id)

      for (const other of others) {
        if (manifest.downloadPageUrl) {
          expect(manifest.downloadPageUrl, manifest.id).not.toBe(other.downloadPageUrl)
        }
      }
    }
  })

  it('quizverse manifest declares TutorX without hosted surface navigation', () => {
    const qv = quizverseBrand.quizverse

    expect(qv.deeptutorRemoteUrl).toMatch(/^https:\/\//)
    expect(qv).not.toHaveProperty('subdomains')
  })

  it('foundrly manifest declares product web + admin portal URLs', () => {
    expect(foundrlyBrand.foundrly.webUrl).toMatch(/^https:\/\//)
    expect(foundrlyBrand.foundrly.adminPortalUrl).toMatch(/^https:\/\//)
  })
})

describe('active brand resolution', () => {
  it('resolves to exactly one brand and the flags agree with it', () => {
    const flags = [IS_IX_AGENCY_BRAND, IS_QUIZVERSE_BRAND, IS_FOUNDRLY_BRAND].filter(Boolean)

    expect(flags).toHaveLength(1)
    expect(BRAND.workspace).toBe(BRAND.id)
    expect(['ix-agency', 'quizverse', 'foundrly']).toContain(DESKTOP_BRAND_ID)
    expect(BRAND.id).toBe(DESKTOP_BRAND_ID)
  })
})
