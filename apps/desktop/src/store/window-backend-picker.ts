import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

import { openNewWindow } from './windows'

export type NewWindowBackendPolicy = 'ask' | 'inherit' | 'primary'

const NEW_WINDOW_BACKEND_POLICY_KEY = 'hermes.desktop.newWindowBackendPolicy'

function validPolicy(value: null | string): value is NewWindowBackendPolicy {
  return value === 'ask' || value === 'inherit' || value === 'primary'
}

function readNewWindowBackendPolicy(): NewWindowBackendPolicy {
  const stored = storedString(NEW_WINDOW_BACKEND_POLICY_KEY)

  return validPolicy(stored) ? stored : 'inherit'
}

export const $newWindowBackendPolicy = atom<NewWindowBackendPolicy>(readNewWindowBackendPolicy())
export const $windowBackendPickerOpen = atom(false)

if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key === NEW_WINDOW_BACKEND_POLICY_KEY) {
      $newWindowBackendPolicy.set(validPolicy(event.newValue) ? event.newValue : 'inherit')
    }
  })
}

export function setNewWindowBackendPolicy(policy: NewWindowBackendPolicy): void {
  $newWindowBackendPolicy.set(policy)
  persistString(NEW_WINDOW_BACKEND_POLICY_KEY, policy)
}

export function openWindowBackendPicker(): void {
  $windowBackendPickerOpen.set(true)
}

export function closeWindowBackendPicker(): void {
  $windowBackendPickerOpen.set(false)
}

export async function requestNewWindow(): Promise<boolean | null> {
  switch ($newWindowBackendPolicy.get()) {
    case 'ask':
      openWindowBackendPicker()

      return null

    case 'primary':
      return openNewWindow('primary')

    default:
      return openNewWindow()
  }
}
