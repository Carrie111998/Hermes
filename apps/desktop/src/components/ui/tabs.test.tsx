import { render, screen } from '@testing-library/react'
import { cleanup } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { Tabs, TabsList, TabsTrigger } from './tabs'

afterEach(cleanup)

describe('Tabs line variant', () => {
  it('uses flat chrome and an active underline instead of a segmented surface', () => {
    render(
      <Tabs defaultValue="overview">
        <TabsList aria-label="Task sections" variant="line">
          <TabsTrigger value="overview" variant="line">
            Overview
          </TabsTrigger>
          <TabsTrigger value="activity" variant="line">
            Activity
          </TabsTrigger>
        </TabsList>
      </Tabs>
    )

    const list = screen.getByRole('tablist', { name: 'Task sections' })
    const active = screen.getByRole('tab', { name: 'Overview' })

    expect(list.className).toContain('border-b')
    expect(list.className).toContain('bg-transparent')
    expect(active.className).toContain('after:bg-(--ui-accent)')
    expect(active.className).not.toContain('data-[state=active]:shadow-xs')
  })
})
