import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { AvatarChip } from '@/components/ui/avatar-chip'

import { brandFor } from './mcp-brands'

afterEach(cleanup)

const CATALOG_BRANDS = [
  'algolia',
  'alltrails',
  'betterstack',
  'buildkite',
  'calendly',
  'circleci',
  'clickup',
  'cloudflare',
  'cloudinary',
  'dropbox',
  'grafana',
  'indeed',
  'miro',
  'mixpanel',
  'postman',
  'railway',
  'robinhood',
  'strava',
  'todoist',
  'trivago',
  'wolfram'
]

describe('MCP catalog brand glyphs', () => {
  it.each(CATALOG_BRANDS)('resolves %s to a real icon instead of a letter fallback', name => {
    const brand = brandFor(name)
    const { container } = render(<AvatarChip brand={brand} name={name} />)

    expect(brand?.Icon).toBeTruthy()
    expect(container.querySelector('svg')).toBeTruthy()
  })

  it('still returns no brand for an unknown MCP server', () => {
    expect(brandFor('private-company-mcp')).toBeNull()
  })
})
