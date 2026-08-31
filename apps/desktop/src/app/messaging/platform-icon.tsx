import {
  SiApple,
  SiBilibili,
  SiDiscord,
  SiGmail,
  SiHomeassistant,
  SiMatrix,
  SiMattermost,
  SiQq,
  SiSignal,
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

// DingTalk and WeCom are absent from Simple Icons, but both marks are available
// in permissively licensed icon sets already represented by Desktop's icon
// dependencies: Tabler (MIT) and Tencent TDesign (MIT), respectively.
function DingTalkIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg
      fill="none"
      stroke="currentColor"
      strokeLinecap="round"
      strokeLinejoin="round"
      strokeWidth="2"
      viewBox="0 0 24 24"
      {...props}
    >
      <path d="M21 12a9 9 0 1 1-18 0a9 9 0 0 1 18 0" />
      <path d="m8 7.5 7.02 2.632a1 1 0 0 1 .567 1.33L14.5 14H16l-5 4 1-4c-3.1.03-3.114-3.139-4-6.5" />
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
// glyph in its native color on top of a soft tint. The fallback monogram uses
// the same hex to keep visual consistency.
type IconKind = 'brand' | 'generic'

interface PlatformIconSpec {
  Icon?: ComponentType<SVGProps<SVGSVGElement>>
  color: string
  kind: IconKind
  monogram?: string
}

const PLATFORM_ICONS: Record<string, PlatformIconSpec> = {
  telegram: { Icon: SiTelegram, color: '#26A5E4', kind: 'brand' },
  discord: { Icon: SiDiscord, color: '#5865F2', kind: 'brand' },
  // Slack removed from Simple Icons by Salesforce request — letter monogram.
  slack: { color: '#4A154B', kind: 'brand', monogram: 'S' },
  mattermost: { Icon: SiMattermost, color: '#0058CC', kind: 'brand' },
  matrix: { Icon: SiMatrix, color: '#000000', kind: 'brand' },
  signal: { Icon: SiSignal, color: '#3A76F0', kind: 'brand' },
  whatsapp: { Icon: SiWhatsapp, color: '#25D366', kind: 'brand' },
  bluebubbles: { Icon: SiApple, color: '#0BD318', kind: 'brand' },
  photon: { Icon: PhotonIcon, color: '#6366F1', kind: 'brand' },
  homeassistant: { Icon: SiHomeassistant, color: '#18BCF2', kind: 'brand' },
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
