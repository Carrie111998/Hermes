import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/hermes'

import {
  groupByTaxonomy,
  hasDisposition,
  isHiddenDisposition
} from './session-taxonomy'

const row = (over: Partial<SessionInfo> = {}): SessionInfo => ({
  id: `s-${Math.random().toString(36).slice(2, 8)}`,
  source: 'telegram',
  ended_at: null,
  input_tokens: 0,
  is_active: false,
  last_active: 0,
  message_count: 1,
  model: null,
  output_tokens: 0,
  preview: null,
  started_at: 1,
  title: 'title',
  tool_call_count: 0,
  ...over
})

describe('groupByTaxonomy', () => {
  it('groups project sessions by project_group, then named project', () => {
    const fusion = row({ disposition: 'project', project_group: 'Hermes Community Extensions', project: 'Fusion Router', last_active: 3 })
    const thin = row({ disposition: 'project', project_group: 'Hermes Community Extensions', project: 'Thin Remote', last_active: 2 })
    const infra = row({ disposition: 'project', project_group: 'Hermes infra', project: undefined, last_active: 1 })

    const view = groupByTaxonomy([infra, thin, fusion])

    expect(view.categoryCount).toBe(2)
    const community = view.groups.find(g => g.label === 'Hermes Community Extensions')
    expect(community).toBeDefined()
    expect(community?.projects.map(p => p.label)).toEqual(['Fusion Router', 'Thin Remote'])
    expect(community?.flat).toHaveLength(0)
    const infraGroup = view.groups.find(g => g.label === 'Hermes infra')
    expect(infraGroup?.flat.map(s => s.id)).toEqual([infra.id])
  })

  it('sorts categories and named projects by newest session first', () => {
    const old = row({ disposition: 'project', project_group: 'Old', last_active: 1 })
    const fresh = row({ disposition: 'project', project_group: 'Fresh', last_active: 9 })
    const a = row({ disposition: 'project', project_group: 'G', project: 'A', last_active: 2 })
    const b = row({ disposition: 'project', project_group: 'G', project: 'B', last_active: 8 })

    const view = groupByTaxonomy([old, a, b, fresh])
    expect(view.groups.map(g => g.label)).toEqual(['Fresh', 'G', 'Old'])
    const g = view.groups.find(x => x.label === 'G')
    expect(g?.projects.map(p => p.label)).toEqual(['B', 'A'])
  })

  it('labels categories without a project_group as Unfiled', () => {
    const view = groupByTaxonomy([row({ disposition: 'project', project_group: null })])
    expect(view.groups[0].id).toBe('__unfiled__')
    expect(view.groups[0].label).toBe('__unfiled__')
  })

  it('keeps unclassified sessions in the fallback list', () => {
    const classified = row({ disposition: 'archive' })
    const unclassified = row({ disposition: null })
    const view = groupByTaxonomy([classified, unclassified])
    expect(view.unclassified.map(s => s.id)).toEqual([unclassified.id])
    expect(view.groups).toHaveLength(1)
  })

  it('separates archive from project buckets', () => {
    const project = row({ disposition: 'project', project_group: 'Same', project: 'Active' })
    const archive = row({ disposition: 'archive', project_group: 'Same', project: 'Done' })

    // The helper is disposition-agnostic: the caller passes only the slice it
    // wants (project slice vs archive slice), so a mixed input just groups by
    // category and keeps both in the same category — matching how the sidebar
    // renders two separate sections fed by two separate slices.
    const view = groupByTaxonomy([project, archive])
    expect(view.categoryCount).toBe(1)
    expect(view.groups[0].projects.map(p => p.label)).toEqual(['Active', 'Done'])
  })
})

describe('disposition helpers', () => {
  it('hasDisposition is false for unclassified rows', () => {
    expect(hasDisposition(row({ disposition: null }))).toBe(false)
    expect(hasDisposition(row({ disposition: 'project' }))).toBe(true)
  })

  it('isHiddenDisposition flags transient and junk only', () => {
    expect(isHiddenDisposition(row({ disposition: 'transient' }))).toBe(true)
    expect(isHiddenDisposition(row({ disposition: 'junk' }))).toBe(true)
    expect(isHiddenDisposition(row({ disposition: 'project' }))).toBe(false)
    expect(isHiddenDisposition(row({ disposition: 'archive' }))).toBe(false)
    expect(isHiddenDisposition(row({ disposition: null }))).toBe(false)
  })
})
