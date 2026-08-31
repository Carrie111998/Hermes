import { normalize } from '@/lib/text'

export interface RankablePaletteItem {
  keywords?: string[]
  label: string
}

export interface RankablePaletteGroup<T extends RankablePaletteItem> {
  heading?: string
  items: T[]
}

interface NormalizedPaletteItem {
  keywords: string[]
  label: string
}

const SCORE_ORDER = [3, 2, 1] as const
const normalizedItems = new WeakMap<RankablePaletteItem, NormalizedPaletteItem>()

function allocationOrder<T extends RankablePaletteItem>(groups: Array<RankablePaletteGroup<T>>): number[] {
  return groups
    .map((group, index) => ({
      index,
      key: normalize(group.heading ?? group.items[0]?.label ?? '')
    }))
    .sort((left, right) => left.key.localeCompare(right.key) || left.index - right.index)
    .map(entry => entry.index)
}

function normalizedItem(item: RankablePaletteItem): NormalizedPaletteItem {
  const cached = normalizedItems.get(item)

  if (cached) {
    return cached
  }

  const value = {
    keywords: (item.keywords ?? []).map(normalize),
    label: normalize(item.label)
  }

  normalizedItems.set(item, value)

  return value
}

function scoreItem(item: RankablePaletteItem, search: string): number {
  const value = normalizedItem(item)

  if (value.label === search) {
    return 3
  }

  if (value.label.startsWith(search)) {
    return 2
  }

  return value.label.includes(search) || value.keywords.some(keyword => keyword.includes(search)) ? 1 : 0
}

/**
 * Rank command-palette groups while bounding the number of mounted rows.
 *
 * Scores have only three levels, so bucketed round-robin selection avoids a
 * full O(n log n) sort, keeps stronger matches from later groups, and shares
 * equal-score capacity across groups deterministically.
 */
export function rankPaletteGroups<T extends RankablePaletteItem>(
  groups: Array<RankablePaletteGroup<T>>,
  search: string,
  maxItems = 60
): Array<RankablePaletteGroup<T>> {
  const normalizedSearch = normalize(search.trim())

  if (!normalizedSearch) {
    return groups
  }

  const limit = Math.max(0, Math.floor(maxItems))

  if (limit === 0) {
    return groups.map(group => ({ ...group, items: [] })).filter(group => group.items.length > 0)
  }

  // A group can contribute at most `limit` rows in total, so retaining only
  // the first `limit` matches in each score bucket is sufficient. The scan is
  // still global across score levels, which means a later exact/prefix match
  // cannot be displaced by an earlier weak match. Once a group has `limit`
  // exact matches, the rest of that group's candidates cannot enter the global
  // result and need not be scored.
  const buckets = new Map<number, T[][]>(SCORE_ORDER.map(score => [score, groups.map(() => [])]))

  let exactMatches = 0

  for (const [groupIndex, group] of groups.entries()) {
    for (const item of group.items) {
      const score = scoreItem(item, normalizedSearch)

      if (score > 0) {
        const bucket = buckets.get(score)?.[groupIndex]

        if (bucket && bucket.length < limit) {
          bucket.push(item)

          if (score === SCORE_ORDER[0]) {
            exactMatches += 1
          }
        }

        if (score === SCORE_ORDER[0] && bucket?.length === limit) {
          break
        }
      }
    }

    if (exactMatches === limit) {
      break
    }
  }

  const selected = groups.map(() => [] as T[])
  const highestScore = groups.map(() => 0)
  const order = allocationOrder(groups)
  let remaining = limit

  for (const score of SCORE_ORDER) {
    const groupBuckets = buckets.get(score) as T[][]
    const cursors = groups.map(() => 0)
    let selectedInPass = true

    while (remaining > 0 && selectedInPass) {
      selectedInPass = false

      for (const groupIndex of order) {
        if (remaining === 0) {
          break
        }

        const item = groupBuckets[groupIndex]?.[cursors[groupIndex] ?? 0]

        if (!item) {
          continue
        }

        selected[groupIndex]?.push(item)
        highestScore[groupIndex] ||= score
        cursors[groupIndex] = (cursors[groupIndex] ?? 0) + 1
        remaining -= 1
        selectedInPass = true
      }
    }

    if (remaining === 0) {
      break
    }
  }

  return groups
    .map((group, index) => ({ group: { ...group, items: selected[index] ?? [] }, index, score: highestScore[index] ?? 0 }))
    .filter(entry => entry.group.items.length > 0)
    .sort((left, right) => right.score - left.score || left.index - right.index)
    .map(entry => entry.group)
}
