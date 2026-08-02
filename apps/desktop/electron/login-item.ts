import type { App, IpcMain } from 'electron'

export interface LoginItemSetOptions {
  openAtLogin: boolean
  openAsHidden?: boolean
}

export function registerLoginItemHandlers(app: App, ipcMain: IpcMain): void {
  // Login-item: launch Hermes Desktop at Windows login, managed by the
  // Electron login-item API (distinct from the gateway autostart managed by
  // `hermes gateway install --start-on-login`).
  ipcMain.handle('hermes:login-item:get', () => app.getLoginItemSettings())

  ipcMain.handle('hermes:login-item:set', (_event, options: LoginItemSetOptions) => {
    app.setLoginItemSettings({
      openAtLogin: options.openAtLogin,
      openAsHidden: options.openAsHidden ?? false,
      // Keep development and packaged builds pointed at the executable that
      // registered the login item. Electron's default-app convention needs
      // the entry script as an argument in development.
      path: process.execPath,
      args: process.defaultApp ? [process.argv[1] ?? ''] : []
    })

    return app.getLoginItemSettings()
  })
}
