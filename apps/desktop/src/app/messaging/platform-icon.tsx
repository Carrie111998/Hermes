import {
  SiDiscord,
  SiGmail,
  SiGooglechat,
  SiHomeassistant,
  SiLine,
  SiMattermost,
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
import feishuIconUrl from '@/assets/brand/feishu-icon.png'
import matrixIconUrl from '@/assets/brand/matrix-icon.svg'
import ntfyIconUrl from '@/assets/brand/ntfy-icon.png'
import raftIconUrl from '@/assets/brand/raft-icon.svg'
import slackIconUrl from '@/assets/brand/slack-logo.svg'
import teamsIconUrl from '@/assets/brand/teams-icon.svg'
import wecomIconUrl from '@/assets/brand/wecom-official.svg'
import yuanbaoIconUrl from '@/assets/brand/yuanbao-official.png'
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

type IconKind = 'brand' | 'generic'

interface PlatformIconSpec {
  Icon?: ComponentType<SVGProps<SVGSVGElement>>
  asset?: string
  assetClassName?: string
  assetMask?: boolean
  color: string
  backgroundColor?: string
  glyphColor?: string
  kind: IconKind
  monochrome?: boolean
  monogram?: string
}

const PLATFORM_ICONS: Record<string, PlatformIconSpec> = {
  telegram: { Icon: SiTelegram, color: '#26A5E4', kind: 'brand' },
  discord: { Icon: SiDiscord, color: '#5865F2', kind: 'brand' },
  slack: { asset: slackIconUrl, color: '#4A154B', kind: 'brand' },
  mattermost: { Icon: SiMattermost, color: '#0058CC', kind: 'brand' },
  matrix: {
    asset: matrixIconUrl,
    assetClassName: 'dark:invert',
    backgroundColor: '#F7F7F5',
    color: '#000000',
    kind: 'brand',
    monochrome: true
  },
  signal: { Icon: SiSignal, color: '#3A76F0', kind: 'brand' },
  whatsapp: { Icon: SiWhatsapp, color: '#25D366', kind: 'brand' },
  bluebubbles: { asset: bluebubblesIconUrl, color: '#27AE60', kind: 'brand' },
  photon: { Icon: PhotonIcon, color: '#6366F1', kind: 'brand' },
  homeassistant: { Icon: SiHomeassistant, color: '#18BCF2', kind: 'brand' },
  google_chat: { Icon: SiGooglechat, color: '#34A853', kind: 'brand' },
  irc: { Icon: IrcIcon, color: '#64748B', kind: 'generic' },
  line: { Icon: SiLine, color: '#00C300', kind: 'brand' },
  ntfy: { asset: ntfyIconUrl, color: '#317F6F', kind: 'brand' },
  raft: { asset: raftIconUrl, color: '#19CBD2', kind: 'brand' },
  simplex: { Icon: SimplexChannelIcon, color: '#1484FF', kind: 'generic' },
  teams: { asset: teamsIconUrl, color: '#6264A7', kind: 'brand' },
  email: { Icon: SiGmail, color: '#EA4335', kind: 'brand' },
  sms: { Icon: MessageSquareText, color: '#F43F5E', kind: 'generic' },
  webhook: { Icon: LinkIcon, color: '#71717A', kind: 'generic' },
  api_server: { Icon: Globe, color: '#64748B', kind: 'generic' },
  weixin: { Icon: SiWechat, color: '#07C160', kind: 'brand' },
  wecom: { asset: wecomIconUrl, color: '#2BAD13', kind: 'brand' },
  wecom_callback: { asset: wecomIconUrl, color: '#2BAD13', kind: 'brand' },
  dingtalk: {
    asset: dingtalkIconUrl,
    assetMask: true,
    backgroundColor: '#0089FF',
    color: '#0089FF',
    glyphColor: '#FFFFFF',
    kind: 'brand'
  },
  qqbot: { Icon: SiQq, color: '#EB1923', kind: 'brand' },
  yuanbao: { asset: yuanbaoIconUrl, color: '#3370FF', kind: 'brand' },
  a2a: { Icon: A2AIcon, color: '#64748B', kind: 'generic' },
  buzz: { Icon: BuzzIcon, color: '#E0A31A', kind: 'generic' },
  feishu: { asset: feishuIconUrl, color: '#3370FF', kind: 'brand' },
  relay: { Icon: RelayIcon, color: '#64748B', kind: 'generic' },
  whatsapp_cloud: { Icon: SiWhatsapp, color: '#25D366', kind: 'brand' },
  msgraph_webhook: { Icon: GraphWebhookIcon, color: '#6264A7', kind: 'generic' }
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
        {spec?.asset ? (
          spec.assetMask ? (
            <span
              aria-hidden
              className="size-[58%] bg-white"
              data-dingtalk-wing=""
              style={{
                maskImage: `url(${spec.asset})`,
                WebkitMaskImage: `url(${spec.asset})`,
                maskPosition: 'center',
                WebkitMaskPosition: 'center',
                maskRepeat: 'no-repeat',
                WebkitMaskRepeat: 'no-repeat',
                maskSize: 'contain',
                WebkitMaskSize: 'contain'
              }}
            />
          ) : (
            <img
              alt=""
              aria-hidden
              className={`size-[58%] object-contain ${spec.assetClassName ?? ''}`}
              src={spec.asset}
            />
          )
        ) : undefined}
      </AvatarChip>
    )
  })
)
