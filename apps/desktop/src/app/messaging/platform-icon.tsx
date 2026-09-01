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
import simplexIconUrl from '@/assets/brand/simplex-icon.svg'
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
  irc: { color: '#64748B', kind: 'brand', monogram: 'IRC' },
  line: { Icon: SiLine, color: '#00C300', kind: 'brand' },
  ntfy: { asset: ntfyIconUrl, color: '#317F6F', kind: 'brand' },
  raft: { asset: raftIconUrl, color: '#19CBD2', kind: 'brand' },
  simplex: { asset: simplexIconUrl, color: '#111827', kind: 'brand', monochrome: true },
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
  a2a: { color: '#64748B', kind: 'brand', monogram: 'A2A' },
  buzz: { color: '#64748B', kind: 'brand', monogram: 'B' },
  feishu: { asset: feishuIconUrl, color: '#3370FF', kind: 'brand' },
  relay: { color: '#64748B', kind: 'brand', monogram: 'R' },
  whatsapp_cloud: { Icon: SiWhatsapp, color: '#25D366', kind: 'brand' },
  msgraph_webhook: { color: '#6264A7', kind: 'brand', monogram: 'M' }
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
