import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Codicon } from '@/components/ui/codicon'
import { Input } from '@/components/ui/input'
import type { OpenRouterEndpoint } from '@/hermes'
import { Loader2 } from '@/lib/icons'
import { normalizeOpenRouterTag, type OpenRouterRoutingDraft } from '@/lib/openrouter-routing'
import { type OpenRouterRoutingSummaryCopy, summarizeOpenRouterRoute } from '@/lib/openrouter-routing-summary'
import { cn } from '@/lib/utils'

interface RoutingCopy {
  allowFallbacks: string
  automatic: string
  blocked: string
  block: string
  discoveryFailed: string
  endpoint: string
  manual: string
  noEndpoints: string
  notCurrentlyReported: string
  providerTag: string
  quantization: string
  refresh: string
  refreshing: string
  selected: string
  subtitle: string
  title: string
  undo: string
  unblock: string
  summaryAutomatic: string
  summarySelectedOnly: string
  summarySelectedPrefer: string
  summaryBlockedOnly: string
  summarySelectedPreferBlocked: string
  summaryEndpointWithQuantization: string
  summaryBlockedJoinTwo: string
  summaryBlockedJoinMany: string
  summaryBlockedListSeparator: string
}

interface OpenRouterRoutingFieldProps {
  copy: RoutingCopy
  draft: OpenRouterRoutingDraft
  endpoints: OpenRouterEndpoint[]
  error: string
  loading: boolean
  manual: boolean
  onDraftChange: (draft: OpenRouterRoutingDraft) => void
  onManualChange: (manual: boolean) => void
  onRefresh: () => void
}

const endpointTag = (endpoint: OpenRouterEndpoint): string => normalizeOpenRouterTag(endpoint.tag)

const endpointName = (endpoint: OpenRouterEndpoint, fallback: string): string =>
  endpoint.provider_name?.trim() || endpointTag(endpoint) || fallback

export function OpenRouterRoutingField({
  copy,
  draft,
  endpoints,
  error,
  loading,
  manual,
  onDraftChange,
  onManualChange,
  onRefresh
}: OpenRouterRoutingFieldProps) {
  const selectedTag = draft.providerTag
  const selectedEndpoint = endpoints.find(endpoint => endpointTag(endpoint) === selectedTag)
  const selectedProviderName = selectedEndpoint ? endpointName(selectedEndpoint, copy.endpoint) : selectedTag

  const blockedProviderNames = draft.blockedTags.map(tag => {
    const endpoint = endpoints.find(candidate => endpointTag(candidate) === tag)

    return endpoint ? endpointName(endpoint, tag) : tag
  })

  const summaryCopy: OpenRouterRoutingSummaryCopy = {
    automatic: copy.summaryAutomatic,
    selectedOnly: copy.summarySelectedOnly,
    selectedPrefer: copy.summarySelectedPrefer,
    blockedOnly: copy.summaryBlockedOnly,
    selectedPreferBlocked: copy.summarySelectedPreferBlocked,
    endpointWithQuantization: copy.summaryEndpointWithQuantization,
    blockedJoinTwo: copy.summaryBlockedJoinTwo,
    blockedJoinMany: copy.summaryBlockedJoinMany,
    blockedListSeparator: copy.summaryBlockedListSeparator
  }

  const summary = summarizeOpenRouterRoute(
    {
      selectedTag,
      selectedProviderName,
      quantization: draft.quantization,
      allowFallbacks: draft.allowFallbacks,
      blockedTags: draft.blockedTags,
      blockedProviderNames
    },
    summaryCopy
  )

  const showManual = manual || !!error || (!loading && endpoints.length === 0)

  const selectEndpoint = (endpoint: OpenRouterEndpoint) => {
    const tag = endpointTag(endpoint)

    if (!tag) {
      return
    }

    onDraftChange({
      ...draft,
      providerTag: tag,
      quantization: endpoint.quantization?.trim() ?? '',
      blockedTags: draft.blockedTags.filter(blocked => blocked !== tag)
    })
  }

  const toggleBlocked = (endpoint: OpenRouterEndpoint) => {
    const tag = endpointTag(endpoint)

    if (!tag) {
      return
    }

    const blocked = draft.blockedTags.includes(tag)
    const selected = selectedTag === tag
    onDraftChange({
      ...draft,
      providerTag: !blocked && selected ? '' : draft.providerTag,
      quantization: !blocked && selected ? '' : draft.quantization,
      blockedTags: blocked ? draft.blockedTags.filter(item => item !== tag) : [...draft.blockedTags, tag]
    })
  }

  return (
    <div className="mt-3 space-y-3 rounded-md border border-border/70 bg-muted/20 p-3">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <p className="text-xs font-medium">{copy.title}</p>
          <p className="text-xs text-muted-foreground">{copy.subtitle}</p>
        </div>
        <Button disabled={loading} onClick={onRefresh} size="sm" variant="text">
          {loading && <Loader2 className="size-3.5 animate-spin" />}
          {loading ? copy.refreshing : copy.refresh}
        </Button>
      </div>

      <div aria-label={copy.endpoint} className="max-h-72 space-y-1 overflow-y-auto pr-1" role="radiogroup">
        <button
          aria-checked={!selectedTag}
          className={cn(
            'flex w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left text-xs',
            !selectedTag ? 'border-primary/50 bg-primary/5' : 'border-border/60 hover:bg-muted/60'
          )}
          onClick={() => onDraftChange({ ...draft, providerTag: '', quantization: '' })}
          role="radio"
          type="button"
        >
          <span
            aria-hidden="true"
            className="flex size-4 items-center justify-center rounded-full border border-current"
          >
            {!selectedTag && <span className="size-2 rounded-full bg-current" />}
          </span>
          <span className="font-medium">{copy.automatic}</span>
        </button>

        {endpoints.map(endpoint => {
          const tag = endpointTag(endpoint)
          const name = endpointName(endpoint, copy.endpoint)
          const blocked = draft.blockedTags.includes(tag)
          const selected = !!tag && selectedTag === tag

          return (
            <div
              className={cn(
                'flex items-center gap-2 rounded-md border px-2.5 py-2 text-xs',
                blocked && 'border-destructive/40 bg-destructive/10 text-destructive',
                selected &&
                  !blocked &&
                  'border-[var(--ui-success-border)] bg-[var(--ui-success-background)] text-[var(--ui-success-foreground)]',
                !blocked && !selected && 'border-border/60'
              )}
              key={`${tag}-${endpoint.quantization ?? ''}`}
            >
              <button
                aria-checked={selected}
                className={cn('flex min-w-0 flex-1 items-center gap-2 text-left', blocked && 'line-through')}
                disabled={!tag}
                onClick={() => selectEndpoint(endpoint)}
                role="radio"
                type="button"
              >
                <span
                  aria-hidden="true"
                  className="flex size-4 shrink-0 items-center justify-center rounded-full border border-current"
                >
                  {selected && <span className="size-2 rounded-full bg-current" />}
                </span>
                <span className="min-w-0 flex-1">
                  <span className="block truncate font-medium">{name}</span>
                  <span className="block truncate font-mono opacity-75">
                    {tag || copy.notCurrentlyReported} · {endpoint.quantization ?? '—'}
                  </span>
                </span>
                {selected && <span className="font-medium no-underline">{copy.selected}</span>}
                {blocked && <span className="font-medium no-underline">{copy.blocked}</span>}
              </button>
              <Button
                aria-label={`${blocked ? copy.unblock : copy.block} ${name}`}
                aria-pressed={blocked}
                onClick={() => toggleBlocked(endpoint)}
                size="sm"
                variant="textStrong"
              >
                <Codicon name={blocked ? 'discard' : 'circle-slash'} />
                {blocked ? copy.undo : copy.block}
              </Button>
            </div>
          )
        })}
      </div>

      {selectedTag && (
        <label className="flex items-center gap-2 text-xs">
          <Checkbox
            aria-label={copy.allowFallbacks}
            checked={draft.allowFallbacks}
            onCheckedChange={checked => onDraftChange({ ...draft, allowFallbacks: checked === true })}
          />
          {copy.allowFallbacks}
        </label>
      )}

      <p className="text-xs text-muted-foreground">{summary}</p>
      {loading && <p className="text-xs text-muted-foreground">{copy.refreshing}</p>}
      {error && (
        <p className="text-xs text-destructive">
          {copy.discoveryFailed}: {error}
        </p>
      )}
      {!loading && !error && endpoints.length === 0 && (
        <p className="text-xs text-muted-foreground">{copy.noEndpoints}</p>
      )}

      {(selectedTag || !!error || endpoints.length === 0) && (
        <Button onClick={() => onManualChange(!manual)} size="sm" variant="textStrong">
          {copy.manual}
        </Button>
      )}

      {showManual && (
        <div className="grid gap-2 sm:grid-cols-2">
          <Input
            aria-label={copy.providerTag}
            onChange={event => {
              const providerTag = normalizeOpenRouterTag(event.target.value)

              onDraftChange({
                ...draft,
                providerTag,
                blockedTags: draft.blockedTags.filter(tag => tag !== providerTag)
              })
            }}
            placeholder={copy.providerTag}
            value={draft.providerTag}
          />
          <Input
            aria-label={copy.quantization}
            onChange={event => onDraftChange({ ...draft, quantization: event.target.value })}
            placeholder={copy.quantization}
            value={draft.quantization}
          />
        </div>
      )}
    </div>
  )
}
