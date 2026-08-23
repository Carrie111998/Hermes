import { closeRightRailTab } from '@/store/preview'

import { forgetPreviewConsole } from './preview-console-store'

/**
 * The single tab-scoped close owner for every preview surface. Keep console
 * cleanup paired with store removal so body chrome cannot strand tab state.
 */
export function closePreviewTab(tabId: string) {
  forgetPreviewConsole(tabId)
  closeRightRailTab(tabId)
}
