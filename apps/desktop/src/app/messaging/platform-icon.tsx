import {
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
  SiTelegram,
  SiWechat,
  SiWhatsapp
} from '@icons-pack/react-simple-icons'
import type { ComponentPropsWithoutRef, ComponentType, SVGProps } from 'react'
import { forwardRef, memo } from 'react'

import blueBubblesIconUrl from '@/assets/brand/bluebubbles-icon.svg?url'
import dingtalkIconUrl from '@/assets/brand/dingtalk-icon.png'
import larkIconUrl from '@/assets/brand/lark-icon.svg?url'
import slackIconUrl from '@/assets/brand/slack-logo.svg?url'
import teamsIconUrl from '@/assets/brand/teams-icon.svg?url'
import wecomIconUrl from '@/assets/brand/wecom-official.svg?url'
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

function RaftIcon(props: React.SVGProps<SVGSVGElement>) {
  return (
    <svg fill="none" viewBox="0 0 113 104" {...props}>
      <path
        d="M112.754 38.7427L108.8 16.3029C108.228 13.0949 106.323 10.5063 103.797 8.4577C102.543 7.44132 98.0803 3.58224 96.2699 2.18472C93.7131 0.183714 90.3622 -0.324477 87.186 0.183714L21.8199 11.8404C16.8809 12.5709 12.9424 13.4761 9.90915 17.5893C6.39945 22.3377 7.43172 26.9273 8.25753 31.6122L11.3384 49.1289C11.4655 49.8435 11.6719 50.5105 11.8943 51.1617C7.57465 51.9239 4.16024 53.4803 2.07983 56.5453C-0.175272 59.8644 -0.349963 62.2942 0.396443 66.5503L3.92202 86.5603C4.82724 90.7688 8.03519 92.9286 12.7042 97.1212C18.1196 101.949 20.2635 104.649 25.679 103.696L96.3017 91.2452C102.892 90.07 107.307 83.7812 106.148 77.1906L102.273 55.1795C102.146 54.4331 101.939 53.7185 101.685 53.0356L102.972 52.7974C109.515 51.6063 113.898 45.3016 112.754 38.7586V38.7427Z"
        fill="#141111"
      />
      <path
        d="M18.3261 87.1956L85.1691 75.4119C87.3607 75.0307 88.8217 72.9345 88.4247 70.7588L85.2168 52.5274C84.8356 50.3358 82.7393 48.8748 80.5636 49.2718C78.3721 49.6529 75.9105 50.0976 74.6718 50.3199C72.7184 50.6693 69.3993 49.4624 68.8276 46.2544L67.7953 40.4102L38.8761 71.9181C37.9708 72.7439 35.9063 73.8397 33.7624 73.3315C31.9043 72.8868 30.5544 71.267 30.2209 69.393L28.2993 58.4828L13.7206 61.0555C11.529 61.4525 10.068 63.5329 10.4491 65.7086L13.6571 83.9241C14.0382 86.1157 16.1345 87.5767 18.3102 87.1797L18.3261 87.1956Z"
        fill="#FFFAEF"
      />
      <path
        d="M20.9147 45.508C21.3117 47.6996 23.3921 49.1607 25.5678 48.7795L31.4914 47.7314C34.2229 47.2549 36.8274 49.0812 37.3038 51.7969L38.3361 57.6411L67.2554 26.1332C68.5417 24.7357 70.5268 24.1799 72.369 24.7198C74.1954 25.2598 75.577 26.7844 75.9105 28.6583L77.8321 39.5368L91.7756 36.9958C93.9513 36.5988 95.3965 34.5184 95.0153 32.3427L91.7279 13.6825C91.3468 11.491 89.2505 10.0299 87.0589 10.4269L20.867 22.1948C18.6754 22.5759 17.2303 24.6722 17.6114 26.8479L20.8988 45.4921L20.9147 45.508Z"
        fill="#FFFAEF"
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
  asset?: string
  color: string
  kind: IconKind
  monogram?: string
}

const PLATFORM_ICONS: Record<string, PlatformIconSpec> = {
  telegram: { Icon: SiTelegram, color: '#26A5E4', kind: 'brand' },
  discord: { Icon: SiDiscord, color: '#5865F2', kind: 'brand' },
  slack: { asset: slackIconUrl, color: '#E01E5A', kind: 'brand' },
  mattermost: { Icon: SiMattermost, color: '#0058CC', kind: 'brand' },
  matrix: { Icon: SiMatrix, color: '#000000', kind: 'brand' },
  signal: { Icon: SiSignal, color: '#3A76F0', kind: 'brand' },
  whatsapp: { Icon: SiWhatsapp, color: '#25D366', kind: 'brand' },
  bluebubbles: { asset: blueBubblesIconUrl, color: '#0099E5', kind: 'brand' },
  photon: { Icon: PhotonIcon, color: '#6366F1', kind: 'brand' },
  homeassistant: { Icon: SiHomeassistant, color: '#18BCF2', kind: 'brand' },
  google_chat: { Icon: SiGooglechat, color: '#34A853', kind: 'brand' },
  irc: { Icon: IrcIcon, color: '#64748B', kind: 'generic' },
  line: { Icon: SiLine, color: '#00C300', kind: 'brand' },
  ntfy: { Icon: SiNtfy, color: '#317F6F', kind: 'brand' },
  raft: { Icon: RaftIcon, color: '#D7A928', kind: 'brand' },
  simplex: { Icon: SimplexChannelIcon, color: '#668BB2', kind: 'generic' },
  teams: { asset: teamsIconUrl, color: '#5F50E2', kind: 'brand' },
  email: { Icon: SiGmail, color: '#EA4335', kind: 'brand' },
  sms: { Icon: MessageSquareText, color: '#F43F5E', kind: 'generic' },
  webhook: { Icon: LinkIcon, color: '#71717A', kind: 'generic' },
  api_server: { Icon: Globe, color: '#64748B', kind: 'generic' },
  weixin: { Icon: SiWechat, color: '#07C160', kind: 'brand' },
  wecom: { asset: wecomIconUrl, color: '#0078FF', kind: 'brand' },
  wecom_callback: { asset: wecomIconUrl, color: '#0078FF', kind: 'brand' },
  dingtalk: { asset: dingtalkIconUrl, color: '#007FFF', kind: 'brand' },
  qqbot: { Icon: SiQq, color: '#EB1923', kind: 'brand' },
  yuanbao: { Icon: YuanbaoIcon, color: '#63A886', kind: 'brand' },
  a2a: { Icon: A2AIcon, color: '#64748B', kind: 'generic' },
  buzz: { Icon: BuzzIcon, color: '#B28A4C', kind: 'generic' },
  feishu: { asset: larkIconUrl, color: '#3370FF', kind: 'brand' },
  relay: { Icon: RelayIcon, color: '#64748B', kind: 'generic' },
  whatsapp_cloud: { Icon: SiWhatsapp, color: '#25D366', kind: 'brand' },
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
        {spec?.asset ? (
          <img alt="" aria-hidden className="size-[58%] object-contain" data-platform-glyph="asset" src={spec.asset} />
        ) : undefined}
      </AvatarChip>
    )
  })
)
