import { useStore } from '@nanostores/react'

import { useSessionView } from '../session-view'

import { TrajectoryPanel } from './trajectory-panel'

export function LiveTrajectoryPanel({ model, provider }: { model: string; provider: string }) {
  const view = useSessionView()
  const messages = useStore(view.$messages)

  return <TrajectoryPanel messages={messages} model={model} provider={provider} />
}
