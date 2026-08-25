import { createContext, useContext, type ReactNode } from 'react'

import type { ModelSelection } from '@/app/shell/model-menu-panel'

export interface ModelControlsContextValue {
  selectModel: (selection: ModelSelection) => Promise<boolean>
}

const ModelControlsContext = createContext<ModelControlsContextValue | null>(null)

/**
 * Hands the shell-rooted ModelSwitchConfirmDialog the live `selectModel` action
 * (owned by contrib wiring's useModelControls). Kept as a tiny context rather
 * than importing wiring directly — the dialog module must stay free of the
 * controller graph so tests can mount it in isolation.
 */
export function ModelControlsProvider({
  children,
  value
}: {
  children: ReactNode
  value: ModelControlsContextValue
}) {
  return <ModelControlsContext.Provider value={value}>{children}</ModelControlsContext.Provider>
}

export function useModelControlsContext(): ModelControlsContextValue | null {
  return useContext(ModelControlsContext)
}
