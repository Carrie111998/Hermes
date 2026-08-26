import type { AnalyticsDailyEntry } from '@/lib/api'

/**
 * Input accounting for the Analytics page.
 *
 * `input_tokens` is stored as the *residual* prompt bucket: the normalizer in
 * `agent/usage_pricing.py` subtracts cache-read and cache-write tokens from the
 * provider's prompt total before persisting it. Displaying that residual as
 * "Input" made days dominated by cache writes look nearly token-free
 * (issue #95707), so every "Input" figure on the page adds cache writes back —
 * i.e. it reports the newly-processed (non-cached-read) prompt input.
 */
export function inputWithCacheWrite(entry: Pick<AnalyticsDailyEntry, 'input_tokens' | 'cache_write_tokens'>): number {
  return (entry.input_tokens ?? 0) + (entry.cache_write_tokens ?? 0)
}

/**
 * Full prompt+output activity for a bucket: newly-processed input (incl. cache
 * writes) plus cache reads plus output. This is the same quantity as
 * `TokenUsage.prompt_tokens + output_tokens` in `agent/usage_pricing.py`.
 */
export function totalProcessedTokens(
  entry: Pick<AnalyticsDailyEntry, 'input_tokens' | 'cache_write_tokens' | 'cache_read_tokens' | 'output_tokens'>,
): number {
  return inputWithCacheWrite(entry) + (entry.cache_read_tokens ?? 0) + (entry.output_tokens ?? 0)
}
