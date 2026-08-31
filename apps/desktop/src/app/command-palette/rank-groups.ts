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

  const buckets = new Map<number, T[][]>(SCORE_ORDER.map(score => [score, groups.map(() => [])]))

  groups.forEach((group, groupIndex) => {
    for (const item of group.items) {
      const score = scoreItem(item, normalizedSearch)

      if (score > 0) {
        buckets.get(score)?.[groupIndex]?.push(item)
      }
    }
  })

  const selected = groups.map(() => [] as T[])
  const highestScore = groups.map(() => 0)
  let remaining = Math.max(0, maxItems)

  for (const score of SCORE_ORDER) {
    const groupBuckets = buckets.get(score) as T[][]
    const cursors = groups.map(() => 0)
    let selectedInPass = true

    while (remaining > 0 && selectedInPass) {
      selectedInPass = false

      for (let groupIndex = 0; groupIndex < groups.length && remaining > 0; groupIndex += 1) {
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
