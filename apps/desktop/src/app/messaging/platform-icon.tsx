import {
  SiDiscord,
  SiGmail,
  SiGooglechat,
  SiHomeassistant,
  SiLine,
  SiMattermost,
  SiNtfy,
  SiQq,
  SiSignal,
  SiTelegram,
  SiWechat,
  SiWhatsapp
} from '@icons-pack/react-simple-icons'
import type { ComponentPropsWithoutRef, ComponentType, SVGProps } from 'react'
import { forwardRef, memo } from 'react'

import bluebubblesIconUrl from '@/assets/brand/bluebubbles-icon.svg'
import dingtalkIconUrl from '@/assets/brand/dingtalk-icon.png'
import larkIconUrl from '@/assets/brand/lark-icon.svg'
import matrixIconUrl from '@/assets/brand/matrix-icon.svg'
import slackIconUrl from '@/assets/brand/slack-logo.svg'
import teamsIconUrl from '@/assets/brand/teams-icon.svg'
import wecomIconUrl from '@/assets/brand/wecom-official.svg'
import { AvatarChip } from '@/components/ui/avatar-chip'
import { Globe, Link as LinkIcon, MessageSquareText } from '@/lib/icons'

function PhotonIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="currentColor" viewBox="0 0 24 24" {...props}>
      <rect height="10" rx="1.25" transform="rotate(15 14 7.5)" width="2.5" x="12.75" y="2.5" />
      <rect height="10" rx="1.25" transform="rotate(15 8 13)" width="2.5" x="6.75" y="8" />
      <rect height="10" rx="1.25" transform="rotate(15 16 18)" width="2.5" x="14.75" y="13" />
    </svg>
  )
}

// Raft's official mark is two offset workspace panels bridged by a diagonal
// handoff. This compact one-color redraw preserves that silhouette at 14px
// without bundling the proprietary website asset or its wordmark treatment.
function RaftIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 24 24" {...props}>
      <path
        d="M5.25 5.75 17.8 3.55c1.05-.18 2 .62 2 1.68v4.1c0 .83-.6 1.54-1.42 1.68l-2.4.42M18.75 18.25 6.2 20.45a1.7 1.7 0 0 1-2-1.68v-4.1c0-.83.6-1.54 1.42-1.68l2.4-.42M8.25 16.75l7.5-9.5"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="2.2"
      />
    </svg>
  )
}

function IrcIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 24 24" {...props}>
      <path d="M5 7.25h14v8.5H9.25L5 19v-3.25H5V7.25Z" stroke="currentColor" strokeLinejoin="round" strokeWidth="2" />
      <path d="M9 11.5h6" stroke="currentColor" strokeLinecap="round" strokeWidth="2" />
    </svg>
  )
}

function A2AIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 24 24" {...props}>
      <circle cx="5.5" cy="12" fill="currentColor" r="2.5" />
      <circle cx="18.5" cy="12" fill="currentColor" r="2.5" />
      <path
        d="M8.5 9.25h7l-2-2M15.5 14.75h-7l2 2"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.8"
      />
    </svg>
  )
}

function BuzzIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 24 24" {...props}>
      <path d="M8.5 8.75h7v6.5h-7z" fill="currentColor" />
      <path
        d="M6 9.75 3.5 8M6 14.25 3.5 16M18 9.75 20.5 8M18 14.25l2.5 1.75"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.8"
      />
      <path
        d="M10 6.25 12 4l2 2.25M10 17.75 12 20l2-2.25"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="1.8"
      />
    </svg>
  )
}

function RelayIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 24 24" {...props}>
      <circle cx="5" cy="12" fill="currentColor" r="2.25" />
      <circle cx="19" cy="7" fill="currentColor" r="2.25" />
      <circle cx="19" cy="17" fill="currentColor" r="2.25" />
      <path d="m7.2 11.2 9.55-3.4M7.2 12.8l9.55 3.4" stroke="currentColor" strokeLinecap="round" strokeWidth="1.8" />
    </svg>
  )
}

function SimplexChannelIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 24 24" {...props}>
      <path
        d="m5 7 5 5-5 5M19 7l-5 5 5 5"
        stroke="currentColor"
        strokeLinecap="round"
        strokeLinejoin="round"
        strokeWidth="2.2"
      />
      <circle cx="12" cy="12" fill="currentColor" r="1.75" />
    </svg>
  )
}

function GraphWebhookIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 24 24" {...props}>
      <circle cx="6" cy="7" fill="currentColor" r="2.25" />
      <circle cx="18" cy="6" fill="currentColor" r="2.25" />
      <circle cx="17" cy="18" fill="currentColor" r="2.25" />
      <circle cx="7" cy="17" fill="currentColor" r="2.25" />
      <path
        d="m8 7 7.75-.75M17.6 8.2l-.4 7.55M14.9 17.75 9.2 17M6.5 14.75l-.25-5.5"
        stroke="currentColor"
        strokeLinecap="round"
        strokeWidth="1.7"
      />
    </svg>
  )
}

function YuanbaoIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="currentColor" fillRule="evenodd" viewBox="0 0 24 24" {...props}>
      <path d="M12.014.648c-6.628 0-12 5.09-12 11.367 0 6.277 5.372 11.366 12 11.366s12-5.09 12-11.366c0-6.277-5.372-11.367-12-11.367zm5.849 15.481c-4.305 3.1-10.584 2.523-13.481-1.444-2.86-3.918-1.351-9.703 2.685-13.02-1.866 1.676-2.67 5.01-1.282 6.909 1.471 2.015 4.794 1.746 6.958.113 2.435-1.84 6.036-1.794 7.234.954.91 2.208.067 4.93-2.114 6.487v.001z" />
      <path d="M14.81 14.914a.669.669 0 0 1-.536-.269l-1.02-1.37a.67.67 0 0 1 .005-.807l1.021-1.328a.669.669 0 0 1 1.06.814l-.713.926.72.964a.67.67 0 0 1-.534 1.067l-.002.003zM10.877 12.913c0 1.797-.357 2.135-1.162 2.135-.805 0-1.162-.338-1.162-2.135 0-1.798.357-2.136 1.162-2.136.805 0 1.162.338 1.162 2.136z" />
    </svg>
  )
}

type IconKind = 'brand' | 'generic'

interface PlatformIconSpec {
  Icon?: ComponentType<SVGProps<SVGSVGElement>>
  color: string
  backgroundColor?: string
  glyphColor?: string
  kind: IconKind
  mask?: string
  monochrome?: boolean
  monogram?: string
}

const PLATFORM_ICONS: Record<string, PlatformIconSpec> = {
  telegram: { Icon: SiTelegram, color: '#26A5E4', kind: 'brand' },
  discord: { Icon: SiDiscord, color: '#5865F2', kind: 'brand' },
  slack: { color: '#6B5870', kind: 'brand', mask: slackIconUrl },
  mattermost: { Icon: SiMattermost, color: '#496FA6', kind: 'brand' },
  matrix: {
    backgroundColor: '#F7F7F5',
    color: '#000000',
    kind: 'brand',
    mask: matrixIconUrl,
    monochrome: true
  },
  signal: { Icon: SiSignal, color: '#3A76F0', kind: 'brand' },
  whatsapp: { Icon: SiWhatsapp, color: '#25D366', kind: 'brand' },
  bluebubbles: { color: '#5F8292', kind: 'brand', mask: bluebubblesIconUrl },
  photon: { Icon: PhotonIcon, color: '#6D759E', kind: 'brand' },
  homeassistant: { Icon: SiHomeassistant, color: '#4C9AB0', kind: 'brand' },
  google_chat: { Icon: SiGooglechat, color: '#5C916B', kind: 'brand' },
  irc: { Icon: IrcIcon, color: '#64748B', kind: 'generic' },
  line: { Icon: SiLine, color: '#4E9B79', kind: 'brand' },
  ntfy: { Icon: SiNtfy, color: '#5D8E84', kind: 'brand' },
  raft: { Icon: RaftIcon, color: '#7A685D', kind: 'brand' },
  simplex: { Icon: SimplexChannelIcon, color: '#668BB2', kind: 'generic' },
  teams: { color: '#74789E', kind: 'brand', mask: teamsIconUrl },
  email: { Icon: SiGmail, color: '#EA4335', kind: 'brand' },
  sms: { Icon: MessageSquareText, color: '#F43F5E', kind: 'generic' },
  webhook: { Icon: LinkIcon, color: '#71717A', kind: 'generic' },
  api_server: { Icon: Globe, color: '#64748B', kind: 'generic' },
  weixin: { Icon: SiWechat, color: '#3A9B6D', kind: 'brand' },
  wecom: { color: '#5B9A63', kind: 'brand', mask: wecomIconUrl },
  wecom_callback: { color: '#5B9A63', kind: 'brand', mask: wecomIconUrl },
  dingtalk: {
    color: '#5F89B5',
    glyphColor: '#5F89B5',
    kind: 'brand',
    mask: dingtalkIconUrl
  },
  qqbot: { Icon: SiQq, color: '#B45D66', kind: 'brand' },
  yuanbao: { Icon: YuanbaoIcon, color: '#63A886', kind: 'brand' },
  a2a: { Icon: A2AIcon, color: '#64748B', kind: 'generic' },
  buzz: { Icon: BuzzIcon, color: '#B28A4C', kind: 'generic' },
  feishu: { color: '#6689B2', kind: 'brand', mask: larkIconUrl },
  relay: { Icon: RelayIcon, color: '#64748B', kind: 'generic' },
  whatsapp_cloud: { Icon: SiWhatsapp, color: '#5A9B78', kind: 'brand' },
  msgraph_webhook: { Icon: GraphWebhookIcon, color: '#74789E', kind: 'generic' }
}

interface PlatformAvatarProps extends Omit<ComponentPropsWithoutRef<'span'>, 'children'> {
  platformId: string
  platformName: string
}

export const PlatformAvatar = memo(
  forwardRef<HTMLSpanElement, PlatformAvatarProps>(function PlatformAvatar(
    { className, platformId, platformName, ...rest },
    ref
  ) {
    const spec = PLATFORM_ICONS[platformId]

    return (
      <AvatarChip brand={spec} className={className} name={platformName} ref={ref} {...rest}>
        {spec?.mask ? (
          <span
            aria-hidden
            className="size-[58%]"
            data-platform-glyph="mask"
            style={{
              backgroundColor: spec.glyphColor ?? spec.color,
              maskImage: `url(${spec.mask})`,
              WebkitMaskImage: `url(${spec.mask})`,
              maskPosition: 'center',
              WebkitMaskPosition: 'center',
              maskRepeat: 'no-repeat',
              WebkitMaskRepeat: 'no-repeat',
              maskSize: 'contain',
              WebkitMaskSize: 'contain'
            }}
          />
        ) : undefined}
      </AvatarChip>
    )
  })
)
