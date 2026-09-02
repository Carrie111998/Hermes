/**
 * Curated brand glyphs for MCP server names — extracted from the mcp-tab's
 * avatar (its `MCP_BRAND_ICONS`) the moment a second surface (the composer
 * suggestion pills / inline setup card) needed the same identity ladder.
 *
 * This is the first rung only. Everything below it — the endpoint's own
 * favicon, then the initial — lives in `components/ui/connector-logo`, which
 * is what a surface should render when it wants a mark rather than a glyph.
 * Either way we never ask a third-party favicon service: an MCP URL can be a
 * private host, and that lookup would leak the hostname off-box.
 */
import {
  SiAirtable,
  SiAsana,
  SiAtlassian,
  SiDatadog,
  SiFigma,
  SiGithub,
  SiGitlab,
  SiHuggingface,
  SiIntercom,
  SiLinear,
  SiN8n,
  SiNetlify,
  SiNotion,
  SiPaypal,
  SiPostgresql,
  SiSentry,
  SiSquare,
  SiStripe,
  SiSupabase,
  SiUnrealengine,
  SiVercel,
  SiWebflow,
  SiZapier
} from '@icons-pack/react-simple-icons'
import type { ComponentType, SVGProps } from 'react'

export interface McpBrand {
  Icon: ComponentType<SVGProps<SVGSVGElement>>
  color: string
  /** The official mark is black/white (GitHub, Vercel, Notion): render it in
   *  `currentColor` so it follows the theme instead of vanishing on dark. The
   *  `color` stays for tint backgrounds (the avatar chip), never the glyph. */
  monochrome?: boolean
}

export const MCP_BRAND_ICONS: Record<string, McpBrand> = {
  airtable: { Icon: SiAirtable, color: '#609FB7' },
  asana: { Icon: SiAsana, color: '#BF7D7D' },
  atlassian: { Icon: SiAtlassian, color: '#426394' },
  datadog: { Icon: SiDatadog, color: '#684D89' },
  figma: { Icon: SiFigma, color: '#B5705B' },
  github: { Icon: SiGithub, color: '#181717', monochrome: true },
  gitlab: { Icon: SiGitlab, color: '#BB8367' },
  hugging_face: { Icon: SiHuggingface, color: '#B9A864' },
  huggingface: { Icon: SiHuggingface, color: '#B9A864' },
  intercom: { Icon: SiIntercom, color: '#79C3BC' },
  linear: { Icon: SiLinear, color: '#7B81B5' },
  n8n: { Icon: SiN8n, color: '#BC7989' },
  netlify: { Icon: SiNetlify, color: '#42948D' },
  notion: { Icon: SiNotion, color: '#000000', monochrome: true },
  paypal: { Icon: SiPaypal, color: '#425F94' },
  postgres: { Icon: SiPostgresql, color: '#6F80B3' },
  postgresql: { Icon: SiPostgresql, color: '#6F80B3' },
  sentry: { Icon: SiSentry, color: '#594D89' },
  square: { Icon: SiSquare, color: '#3E4348', monochrome: true },
  stripe: { Icon: SiStripe, color: '#7D79C3' },
  supabase: { Icon: SiSupabase, color: '#65A98A' },
  'unreal-engine': { Icon: SiUnrealengine, color: '#0E1128', monochrome: true },
  vercel: { Icon: SiVercel, color: '#000000', monochrome: true },
  webflow: { Icon: SiWebflow, color: '#567BB3' },
  zapier: { Icon: SiZapier, color: '#B06B4F' }
}

/** Inline-glyph color for a brand: source marks are rendered as one quiet,
 * low-saturation color; black/white marks inherit the surrounding text color. */
export const brandGlyphStyle = (brand: McpBrand): { color: string } | undefined =>
  brand.monochrome ? undefined : { color: brand.color }

/** The same brand under every spelling a connector arrives with: catalog slug
 *  (`unreal-engine`), registry id (`unreal_engine`), display name (`Unreal
 *  Engine`). Compare on letters and digits alone so none of them miss. */
const squash = (value: string): string => value.toLowerCase().replace(/[^a-z0-9]/g, '')

export const brandFor = (name: string): McpBrand | null => {
  const target = squash(name)

  if (!target) {
    return null
  }

  const entries = Object.entries(MCP_BRAND_ICONS)

  return (
    entries.find(([key]) => squash(key) === target)?.[1] ??
    entries.find(([key]) => target.includes(squash(key)))?.[1] ??
    null
  )
}
