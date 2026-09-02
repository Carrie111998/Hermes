import { cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { AvatarChip } from '@/components/ui/avatar-chip'

import { brandFor } from './mcp-brands'

afterEach(cleanup)

const CATALOG_BRANDS = [
  'airtable',
  'algolia',
  'alltrails',
  'asana',
  'atlassian',
  'betterstack',
  'buildkite',
  'calendly',
  'circleci',
  'clickup',
  'cloudflare',
  'cloudinary',
  'datadog',
  'dropbox',
  'figma',
  'gitlab',
  'grafana',
  'hugging_face',
  'indeed',
  'intercom',
  'linear',
  'miro',
  'mixpanel',
  'n8n',
  'netlify',
  'notion',
  'paypal',
  'postman',
  'prisma-postgres',
  'railway',
  'robinhood',
  'sentry',
  'square',
  'strava',
  'stripe',
  'supabase',
  'todoist',
  'trivago',
  'unreal-engine',
  'vercel',
  'webflow',
  'wolfram',
  'wordpress-com'
]

const CATALOG_FALLBACKS = [
  'amplitude',
  'attio',
  'aws-knowledge',
  'canva',
  'close',
  'comfy-cloud',
  'context7',
  'craft',
  'deepwiki',
  'fireflies',
  'gamma',
  'globalping',
  'kiwi',
  'klaviyo',
  'microsoft-learn',
  'monday',
  'motherduck',
  'neon',
  'plaid',
  'semgrep',
  'twelve-data',
  'twilio-docs'
]

describe('MCP catalog brand glyphs', () => {
  it.each(CATALOG_BRANDS)('resolves %s to a real icon instead of a letter fallback', name => {
    const brand = brandFor(name)
    const { container } = render(<AvatarChip brand={brand} name={name} />)

    expect(brand?.Icon).toBeTruthy()
    expect(container.querySelector('svg')).toBeTruthy()
  })

  it.each(CATALOG_FALLBACKS)('keeps %s on the honest monogram fallback', name => {
    const brand = brandFor(name)
    const { container } = render(<AvatarChip brand={brand} name={name} />)

    expect(brand).toBeNull()
    expect(container.querySelector('svg')).toBeNull()
    expect(container.textContent).toBe(name.charAt(0).toUpperCase())
  })

  it('still returns no brand for an unknown MCP server', () => {
    expect(brandFor('private-company-mcp')).toBeNull()
  })
})
