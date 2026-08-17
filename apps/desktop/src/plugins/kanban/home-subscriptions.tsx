import { Button, host, Skeleton, Switch, useMutation, useQuery, useQueryClient, useValue } from '@hermes/plugin-sdk'
import { useEffect } from 'react'

import {
  $boardSlug,
  $homeChannelsSupported,
  fetchHomeChannels,
  homeChannelsKey,
  subscribeHome,
  unsubscribeHome
} from './api'
import {
  type KanbanHomeChannel,
  type KanbanHomeChannelsResponse,
  SEVERITY_TONE,
  type StaleHomeSubscription
} from './types'
import { Callout, errText, Section, useKanban } from './ui'

function platformLabel(platform: string): string {
  return platform
    .split(/[-_]/)
    .filter(Boolean)
    .map(part => `${part.charAt(0).toUpperCase()}${part.slice(1)}`)
    .join(' ')
}

function hasHomeSource(channel: KanbanHomeChannel): boolean {
  if (channel.subscription_state == null) {
    return channel.subscribed
  }

  return channel.subscription_state === 'home' || channel.subscription_state === 'home_and_other'
}

function optimisticResponse(
  response: KanbanHomeChannelsResponse | undefined,
  platform: string,
  enabled: boolean
): KanbanHomeChannelsResponse | undefined {
  if (!response) {
    return response
  }

  return {
    ...response,
    home_channels: response.home_channels.map(channel => {
      if (channel.platform !== platform) {
        return channel
      }

      const hasOther = channel.subscription_state === 'other' || channel.subscription_state === 'home_and_other'

      return {
        ...channel,
        subscribed: enabled || hasOther,
        subscription_state: enabled ? (hasOther ? 'home_and_other' : 'home') : hasOther ? 'other' : 'none'
      }
    }),
    stale_home_subscriptions: response.stale_home_subscriptions?.filter(stale => stale.platform !== platform)
  }
}

function restorePlatform(
  current: KanbanHomeChannelsResponse | undefined,
  previous: KanbanHomeChannelsResponse,
  platform: string
): KanbanHomeChannelsResponse {
  if (!current) {
    return previous
  }

  const previousChannel = previous.home_channels.find(channel => channel.platform === platform)
  const otherChannels = current.home_channels.filter(channel => channel.platform !== platform)
  const currentOtherStale = (current.stale_home_subscriptions ?? []).filter(stale => stale.platform !== platform)
  const previousPlatformStale = (previous.stale_home_subscriptions ?? []).filter(stale => stale.platform === platform)

  return {
    ...current,
    home_channels: previousChannel ? [...otherChannels, previousChannel] : otherChannels,
    stale_home_subscriptions: [...currentOtherStale, ...previousPlatformStale]
  }
}

interface MutationContext {
  previous?: KanbanHomeChannelsResponse
}

function useHomeMutation(taskId: string, platform: string) {
  const k = useKanban()
  const qc = useQueryClient()
  const slug = useValue($boardSlug)
  const key = homeChannelsKey(slug, taskId)
  const mutationKey = ['kanban', 'home-channel-mutation', slug, taskId] as const

  return useMutation<unknown, unknown, boolean, MutationContext>({
    mutationKey,
    mutationFn: enabled => (enabled ? subscribeHome(taskId, platform, slug) : unsubscribeHome(taskId, platform, slug)),
    onMutate: async enabled => {
      await qc.cancelQueries({ queryKey: key })
      const previous = qc.getQueryData<KanbanHomeChannelsResponse>(key)
      qc.setQueryData(key, optimisticResponse(previous, platform, enabled))

      return { previous }
    },
    onError: (error, _enabled, context) => {
      if (context?.previous) {
        qc.setQueryData<KanbanHomeChannelsResponse | undefined>(key, current =>
          restorePlatform(current, context.previous!, platform)
        )
      }

      host.notify({
        kind: 'error',
        message: k.homeChannelsUpdateError(platformLabel(platform), errText(error))
      })
    },
    onSettled: () => {
      if (qc.isMutating({ exact: true, mutationKey }) === 1) {
        void qc.invalidateQueries({ queryKey: key })
      }
    }
  })
}

function HomeChannelRow({
  channel,
  offline,
  taskId
}: {
  channel: KanbanHomeChannel
  offline: boolean
  taskId: string
}) {
  const k = useKanban()
  const mutation = useHomeMutation(taskId, channel.platform)
  const platform = platformLabel(channel.platform)
  const checked = hasHomeSource(channel)
  const hasOther = channel.subscription_state === 'other' || channel.subscription_state === 'home_and_other'

  return (
    <div className="flex min-h-13 items-center gap-2.5 py-1">
      <div className="min-w-0 flex-1">
        <div className="truncate text-[0.75rem] font-medium text-(--ui-text-secondary)">{platform}</div>
        <div className="truncate text-[0.6875rem] text-(--ui-text-quaternary)">{channel.name}</div>
        {hasOther && <div className="text-[0.625rem] text-(--ui-text-quaternary)">{k.homeChannelsOrigin}</div>}
      </div>
      {mutation.isPending && (
        <span aria-live="polite" className="text-[0.625rem] text-(--ui-text-quaternary)">
          {k.homeChannelsSaving}
        </span>
      )}
      <Switch
        aria-label={k.homeChannelAria(channel.name, platform)}
        checked={checked}
        disabled={offline || mutation.isPending}
        onCheckedChange={enabled => mutation.mutate(enabled)}
        size="xs"
      />
    </div>
  )
}

function StaleHomeCallout({
  current,
  offline,
  stale,
  taskId
}: {
  current?: KanbanHomeChannel
  offline: boolean
  stale: StaleHomeSubscription
  taskId: string
}) {
  const k = useKanban()
  const mutation = useHomeMutation(taskId, stale.platform)
  const platform = platformLabel(stale.platform)
  const previousHome = k.homePrevious

  return (
    <Callout title={k.homeChanged(platform)} tone={SEVERITY_TONE.warning}>
      <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">
        {current ? k.homeChangedMove(previousHome, current.name) : k.homeChangedStop(previousHome)}
      </p>
      <div className="flex flex-wrap gap-1.5">
        {current && (
          <Button
            disabled={offline || mutation.isPending}
            onClick={() => mutation.mutate(true)}
            size="xs"
            variant="secondary"
          >
            {k.homeMove(current.name)}
          </Button>
        )}
        <Button
          disabled={offline || mutation.isPending}
          onClick={() => mutation.mutate(false)}
          size="xs"
          variant="outline"
        >
          {k.homeStop}
        </Button>
      </div>
    </Callout>
  )
}

function LoadingRows() {
  return (
    <div className="flex flex-col gap-2" data-testid="home-channels-loading">
      {[0, 1].map(index => (
        <div className="flex min-h-13 items-center gap-2.5" key={index}>
          <div className="flex flex-1 flex-col gap-1.5">
            <Skeleton className="h-3 w-20" />
            <Skeleton className="h-2.5 w-32" />
          </div>
          <Skeleton className="h-4 w-7" />
        </div>
      ))}
    </div>
  )
}

export function HomeChannelNotifications({ taskId }: { taskId: string }) {
  const k = useKanban()
  const slug = useValue($boardSlug)
  const gatewayState = useValue(host.state.gateway)
  const supported = useValue($homeChannelsSupported)

  const query = useQuery({
    enabled: supported !== false && gatewayState === 'open',
    queryFn: () => fetchHomeChannels(taskId, slug),
    queryKey: homeChannelsKey(slug, taskId),
    retry: false
  })

  useEffect(() => {
    if (query.data) {
      $homeChannelsSupported.set(true)
    }
  }, [query.data])

  if (supported === false) {
    return null
  }

  const offline = gatewayState !== 'open'

  const homes = [...(query.data?.home_channels ?? [])].sort((a, b) =>
    platformLabel(a.platform).localeCompare(platformLabel(b.platform))
  )

  const stale = query.data?.stale_home_subscriptions ?? []
  const stalePlatforms = new Set(stale.map(item => item.platform))

  return (
    <Section label={k.homeChannels}>
      <p className="text-[0.6875rem] leading-relaxed text-(--ui-text-quaternary)">{k.homeChannelsHelp}</p>
      {query.isPending && !offline ? (
        <LoadingRows />
      ) : query.error && !query.data ? (
        <Callout icon="error" title={k.homeChannelsUnavailable} tone="var(--destructive, #f87171)">
          <p className="text-[0.71rem] leading-relaxed text-(--ui-text-secondary)">{k.homeChannelsUnavailableBody}</p>
          <Button onClick={() => void query.refetch()} size="xs" variant="outline">
            {k.homeChannelsRetry}
          </Button>
        </Callout>
      ) : (
        <div className="flex flex-col gap-1.5">
          {offline && <p className="text-[0.6875rem] text-(--ui-text-quaternary)">{k.homeChannelsOffline}</p>}
          {stale.map(item => (
            <StaleHomeCallout
              current={homes.find(home => home.platform === item.platform)}
              key={`${item.platform}:${item.chat_id}:${item.thread_id}`}
              offline={offline}
              stale={item}
              taskId={taskId}
            />
          ))}
          {homes
            .filter(home => !stalePlatforms.has(home.platform))
            .map(home => (
              <HomeChannelRow channel={home} key={home.platform} offline={offline} taskId={taskId} />
            ))}
          {!offline && homes.length === 0 && stale.length === 0 && (
            <p className="text-[0.75rem] text-(--ui-text-quaternary)">{k.homeChannelsEmpty}</p>
          )}
        </div>
      )}
    </Section>
  )
}
