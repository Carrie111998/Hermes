'use client'

import { useI18n } from '@/i18n'
import { PrettyLink } from '@/lib/external-link'

import type { TweetEmbed } from './providers/types'

/**
 * X/Twitter does not expose a provider iframe URL that we can safely derive
 * from the post descriptor. Keep the rich card as an explicit link instead of
 * loading the provider widget script into the privileged Desktop renderer.
 * Instagram uses its fixed iframe URL through FrameEmbedRenderer.
 */
export default function SocialEmbedRenderer({ descriptor }: { descriptor: TweetEmbed }) {
  const { t } = useI18n()
  const copy = t.assistant.embeds

  return (
    <div
      className="flex min-h-32 w-full flex-col items-center justify-center gap-2 rounded-lg border border-dashed border-(--ui-stroke-tertiary) bg-(--ui-bg-quinary)/30 p-4"
      data-provider={descriptor.provider}
    >
      <span className="text-xs text-(--ui-text-tertiary)">{copy.openPostOn(descriptor.label)}</span>
      <PrettyLink fallbackLabel={copy.openPost(descriptor.label)} href={descriptor.sourceUrl} />
    </div>
  )
}
