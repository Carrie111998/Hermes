import { registerPlugin } from '@capacitor/core'

export type MobileQuickAction = 'newTask' | 'wakeToggle'

interface MobileQuickActionPlugin {
  addListener(eventName: 'quickAction', listener: (event: { action?: MobileQuickAction }) => void): Promise<{ remove: () => Promise<void> }>
  getPending(): Promise<{ action?: MobileQuickAction }>
}

const MobileQuickAction = registerPlugin<MobileQuickActionPlugin>('MobileQuickAction')

function parse(value: { action?: MobileQuickAction }): MobileQuickAction | null {
  return value.action === 'newTask' || value.action === 'wakeToggle' ? value.action : null
}

/** Consume a user-tapped widget/notification action exactly once. */
export async function consumePendingMobileQuickAction(): Promise<MobileQuickAction | null> {
  try {
    return parse(await MobileQuickAction.getPending())
  } catch {
    return null
  }
}

export async function listenForMobileQuickActions(onAction: (action: MobileQuickAction) => void): Promise<() => void> {
  try {
    const handle = await MobileQuickAction.addListener('quickAction', event => {
      const action = parse(event)
      if (action) onAction(action)
    })
    return () => void handle.remove()
  } catch {
    return () => undefined
  }
}
