import { describe, expect, it } from 'vitest'

import { brandFor, brandGlyphStyle, MCP_BRAND_ICONS } from './mcp-brands'

const EXPECTED_MUTED_COLORS = {
  airtable: '#609FB7',
  asana: '#BF7D7D',
  atlassian: '#426394',
  datadog: '#684D89',
  figma: '#B5705B',
  gitlab: '#BB8367',
  hugging_face: '#B9A864',
  huggingface: '#B9A864',
  intercom: '#79C3BC',
  linear: '#7B81B5',
  n8n: '#BC7989',
  netlify: '#42948D',
  paypal: '#425F94',
  postgres: '#6F80B3',
  postgresql: '#6F80B3',
  sentry: '#594D89',
  stripe: '#7D79C3',
  supabase: '#65A98A',
  webflow: '#567BB3',
  zapier: '#B06B4F'
} as const

describe('MCP brand glyphs', () => {
  it('keeps audited Simple Icons while using the shared muted palette', () => {
    for (const [name, color] of Object.entries(EXPECTED_MUTED_COLORS)) {
      const brand = MCP_BRAND_ICONS[name]

      expect(brand.color, name).toBe(color)
      expect(brand.monochrome, name).toBeUndefined()
      expect(brandGlyphStyle(brand), name).toEqual({ color })
      expect(brandFor(`${name} MCP`), name).toEqual(brand)
    }
  })

  it('keeps source-black marks theme-following and monochrome', () => {
    for (const name of ['github', 'notion', 'square', 'unreal-engine', 'vercel']) {
      const brand = MCP_BRAND_ICONS[name]

      expect(brand.monochrome, name).toBe(true)
      expect(brandGlyphStyle(brand), name).toBeUndefined()
    }
  })

  it('keeps aliases on the same visual treatment', () => {
    expect(MCP_BRAND_ICONS.huggingface).toEqual(MCP_BRAND_ICONS.hugging_face)
    expect(MCP_BRAND_ICONS.postgresql).toEqual(MCP_BRAND_ICONS.postgres)
    expect(brandFor('Linear App')).toBe(MCP_BRAND_ICONS.linear)
    expect(brandFor('unreal_engine')).toBe(MCP_BRAND_ICONS['unreal-engine'])
  })

  it('does not invent a brand for unknown MCP names', () => {
    expect(brandFor('private-company-server')).toBeNull()
    expect(brandFor('')).toBeNull()
  })
})
