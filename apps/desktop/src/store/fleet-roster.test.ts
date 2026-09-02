import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { DesktopAgentRoster } from '@/global'

import { _resetFleetRosterForTests, refreshFleetRoster } from './fleet-roster'

function deferred<T>() {
  let resolve!: (value: T) => void

  const promise = new Promise<T>(resolvePromise => {
    resolve = resolvePromise
  })

  return { promise, resolve }
}

const emptyRoster: DesktopAgentRoster = { agents: [], sources: [] }

beforeEach(() => {
  _resetFleetRosterForTests()
})

afterEach(() => {
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

it('queues a forced recovery refresh behind an in-flight startup enumeration', async () => {
  const startup = deferred<DesktopAgentRoster>()

  const getAgentRoster = vi.fn().mockImplementationOnce(() => startup.promise).mockResolvedValue(emptyRoster)

  ;(window as { hermesDesktop?: unknown }).hermesDesktop = { getAgentRoster }

  const initialRefresh = refreshFleetRoster()
  const recoveryRefresh = refreshFleetRoster({ force: true })

  expect(getAgentRoster).toHaveBeenCalledTimes(1)
  startup.resolve(emptyRoster)
  await Promise.all([initialRefresh, recoveryRefresh])
  await vi.waitFor(() => expect(getAgentRoster).toHaveBeenCalledTimes(2))
})
