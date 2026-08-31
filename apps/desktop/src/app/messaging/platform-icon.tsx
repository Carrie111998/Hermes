import {
  SiApple,
  SiBilibili,
  SiDiscord,
  SiGmail,
  SiGooglechat,
  SiHomeassistant,
  SiLine,
  SiMatrix,
  SiMattermost,
  SiNtfy,
  SiQq,
  SiSignal,
  SiSimplex,
  SiTelegram,
  SiWechat,
  SiWhatsapp
} from '@icons-pack/react-simple-icons'
import type { ComponentPropsWithoutRef, ComponentType, SVGProps } from 'react'
import { forwardRef, memo } from 'react'

import { AvatarChip } from '@/components/ui/avatar-chip'
import { Globe, Link as LinkIcon, MessageSquareText } from '@/lib/icons'

// ---------------------------------------------------------------------------
// Photon brand icon — three diagonal rounded bars (the Photon logo mark).
// Rendered at ~14 px inside the PlatformAvatar so the bars are kept thick
// enough to stay legible. At small sizes the bars blend into a distinctive
// silhouette; the wide triangular spacing preserves the logo's identity.
// ---------------------------------------------------------------------------
function PhotonIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="currentColor" viewBox="0 0 24 24" {...props}>
      <rect height="10" rx="1.25" transform="rotate(15 14 7.5)" width="2.5" x="12.75" y="2.5" />
      <rect height="10" rx="1.25" transform="rotate(15 8 13)" width="2.5" x="6.75" y="8" />
      <rect height="10" rx="1.25" transform="rotate(15 16 18)" width="2.5" x="14.75" y="13" />
    </svg>
  )
}

// DingTalk's compact list mark uses the same small colored chip as the other
// platforms, but keeps the brand's filled center at this size. The outer ring
// preserves the circular silhouette; the white glyph prevents it becoming a
// generic blue dot.
function DingTalkIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 24 24" {...props}>
      <circle cx="12" cy="12" r="9" stroke="currentColor" strokeWidth="1.75" />
      <circle cx="12" cy="12" fill="currentColor" r="6.25" />
      <path
        d="m8.45 7.8 6.6 2.475a1 1 0 0 1 .568 1.33L14.48 13.9h1.5l-4.7 3.75.92-3.53c-2.27-.22-2.94-2.54-3.75-6.32Z"
        fill="white"
      />
    </svg>
  )
}

function WeComIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="currentColor" viewBox="0 0 24 24" {...props}>
      <path d="M9.95 2C4.47 2 0 5.88 0 10.66c0 2.93 1.67 5.64 4.4 7.25l-.55 2.3a.7.7 0 0 0 .98.79l2.46-1.25c.86.25 1.75.38 2.66.38.71 0 1.42-.08 2.11-.22a3.28 3.28 0 0 1-.43-1.87 11.1 11.1 0 0 1-1.68.13c-.34 0-.68-.02-1.03-.06a9.1 9.1 0 0 1-1.52-.29l-.76-.22-1.23.62.21-.88-.98-.79C2.9 15.15 2 12.91 2 10.66 2 6.98 5.56 4 9.95 4c3.45 0 6.39 1.85 7.49 4.44.24.56.39 1.14.43 1.72a3.3 3.3 0 0 1 2.01.1c-.07-.91-.32-1.82-.76-2.71C17.62 4.36 14.09 2 9.95 2Z" />
      <path d="M18.73 10.1a3.2 3.2 0 0 0-1.94 5.75 5.2 5.2 0 0 1-2.23 2.23 3.2 3.2 0 1 0 4.75 2.8 5.2 5.2 0 0 1 2.22-2.23 3.2 3.2 0 1 0-2.8-8.55Zm0 2a1.2 1.2 0 1 1 0 2.4 1.2 1.2 0 0 1 0-2.4Zm-3.04 4.76a1.2 1.2 0 1 1 0 2.4 1.2 1.2 0 0 1 0-2.4Zm6.11-1.02a1.2 1.2 0 1 1 0 2.4 1.2 1.2 0 0 1 0-2.4Z" />
    </svg>
  )
}

// We render simpleicons.org brand glyphs for platforms whose owners publish a
// usable mark (telegram, discord, matrix, ...). A few brands — Slack, Dingtalk,
// Feishu, WeCom — have been removed from Simple Icons at the brand owner's
// request, so we fall back to a colored letter monogram for those.
//
// `iconColor` is the brand's hex from simpleicons.org so we can paint each
// glyph in its native color on top of a soft tint. Locally curated marks use
// the same contract, which keeps the fallback and brand paths visually aligned.
type IconKind = 'brand' | 'generic'

interface PlatformIconSpec {
  Icon?: ComponentType<SVGProps<SVGSVGElement>>
  color: string
  kind: IconKind
  monochrome?: boolean
  monogram?: string
}

const PLATFORM_ICONS: Record<string, PlatformIconSpec> = {
  telegram: { Icon: SiTelegram, color: '#26A5E4', kind: 'brand' },
  discord: { Icon: SiDiscord, color: '#5865F2', kind: 'brand' },
  // Slack removed from Simple Icons by Salesforce request — letter monogram.
  slack: { color: '#4A154B', kind: 'brand', monogram: 'S' },
  mattermost: { Icon: SiMattermost, color: '#0058CC', kind: 'brand' },
  matrix: { Icon: SiMatrix, color: '#0DBD8B', kind: 'brand' },
  signal: { Icon: SiSignal, color: '#3A76F0', kind: 'brand' },
  whatsapp: { Icon: SiWhatsapp, color: '#25D366', kind: 'brand' },
  bluebubbles: { Icon: SiApple, color: '#0BD318', kind: 'brand' },
  photon: { Icon: PhotonIcon, color: '#6366F1', kind: 'brand' },
  homeassistant: { Icon: SiHomeassistant, color: '#18BCF2', kind: 'brand' },
  google_chat: { Icon: SiGooglechat, color: '#34A853', kind: 'brand' },
  irc: { color: '#64748B', kind: 'brand', monogram: 'IRC' },
  line: { Icon: SiLine, color: '#00C300', kind: 'brand' },
  ntfy: { Icon: SiNtfy, color: '#317F6F', kind: 'brand' },
  raft: { color: '#6366F1', kind: 'brand', monogram: 'R' },
  simplex: { Icon: SiSimplex, color: '#111827', kind: 'brand', monochrome: true },
  teams: { color: '#6264A7', kind: 'brand', monogram: 'T' },
  email: { Icon: SiGmail, color: '#EA4335', kind: 'brand' },
  sms: { Icon: MessageSquareText, color: '#F43F5E', kind: 'generic' },
  webhook: { Icon: LinkIcon, color: '#71717A', kind: 'generic' },
  api_server: { Icon: Globe, color: '#64748B', kind: 'generic' },
  weixin: { Icon: SiWechat, color: '#07C160', kind: 'brand' },
  wecom: { Icon: WeComIcon, color: '#2BAD13', kind: 'brand' },
  wecom_callback: { Icon: WeComIcon, color: '#2BAD13', kind: 'brand' },
  dingtalk: { Icon: DingTalkIcon, color: '#0089FF', kind: 'brand' },
  qqbot: { Icon: SiQq, color: '#EB1923', kind: 'brand' },
  yuanbao: { Icon: SiBilibili, color: '#FB7299', kind: 'brand' }
}

interface PlatformAvatarProps extends Omit<ComponentPropsWithoutRef<'span'>, 'children'> {
  platformId: string
  platformName: string
}

// forwardRef + spreading ...rest is required so a wrapping <Tip> (Radix
// Tooltip's `asChild`) can actually attach its trigger: asChild clones this
// component and injects a ref plus pointer/focus/aria handlers onto it. A
// plain function component with no ref/rest forwarding drops all of that
// silently — the tooltip renders but never opens (#67500).
export const PlatformAvatar = memo(
  forwardRef<HTMLSpanElement, PlatformAvatarProps>(function PlatformAvatar(
    { className, platformId, platformName, ...rest },
    ref
  ) {
    return (
      <AvatarChip
        aria-hidden="true"
        brand={PLATFORM_ICONS[platformId]}
        className={className}
        name={platformName}
        ref={ref}
        {...rest}
      />
    )
  })
)
