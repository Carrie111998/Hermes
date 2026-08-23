import { bindTreeSideVisibility, registerLayoutResetHandler } from '@/components/pane-shell/tree/store'
import { $sidebarOpen, setFileBrowserOpen, setSidebarOpen } from '@/store/layout'

const restoreFilesOnLayoutReset = () => setFileBrowserOpen(true)

/** Wire the titlebar's positional Sessions toggle and Files reset behavior. */
export function wireTitlebarVisibility() {
  bindTreeSideVisibility('left', $sidebarOpen, setSidebarOpen)
  registerLayoutResetHandler(restoreFilesOnLayoutReset)
}
