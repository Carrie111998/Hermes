import { act, fireEvent, render, screen } from '@testing-library/react'
import { Suspense } from 'react'
import { describe, expect, it, vi } from 'vitest'

import { useCommittedActionScope } from './use-committed-action-scope'

describe('useCommittedActionScope', () => {
  it('keeps the committed handler current when a different-scope render suspends', () => {
    const called = vi.fn()
    const suspended = new Promise<void>(() => undefined)

    function Probe({ scope, suspend = false }: { scope: string; suspend?: boolean }) {
      const actionIsCurrent = useCommittedActionScope(scope, false)

      if (suspend) {
        throw suspended
      }

      return <button onClick={() => actionIsCurrent() && called(scope)}>Run {scope}</button>
    }

    const view = render(
      <Suspense fallback={<div>Loading</div>}>
        <Probe scope="session-a" />
      </Suspense>
    )

    act(() => {
      view.rerender(
        <Suspense fallback={<div>Loading</div>}>
          <Probe scope="session-b" suspend />
        </Suspense>
      )
    })

    fireEvent.click(screen.getByText('Run session-a'))

    expect(called).toHaveBeenCalledOnce()
    expect(called).toHaveBeenCalledWith('session-a')
  })
})
