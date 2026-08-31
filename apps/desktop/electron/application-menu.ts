import type { MenuItemConstructorOptions } from 'electron'

/**
 * The macOS Window menu must use Electron's native role rather than a manually
 * equivalent submenu. AppKit recognizes that role as the application's window
 * menu and augments it with system commands such as Move & Resize, including
 * the user's Control+Fn+Arrow tiling shortcuts.
 */
export function applicationWindowMenu(platform: NodeJS.Platform): MenuItemConstructorOptions {
  if (platform === 'darwin') {
    return { role: 'windowMenu' }
  }

  return {
    label: 'Window',
    submenu: [{ role: 'minimize' }, { role: 'close' }]
  }
}
