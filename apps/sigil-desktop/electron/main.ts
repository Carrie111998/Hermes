import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { app, BrowserWindow, nativeTheme } from 'electron'

export const SIGIL_APP_NAME = 'Sigil'
export const SIGIL_BUNDLE_ID = 'com.firecattechnology.sigil'
export const SIGIL_USER_DATA_DIRECTORY = 'Sigil'

app.setName(SIGIL_APP_NAME)
nativeTheme.themeSource = 'dark'

const currentDirectory = path.dirname(fileURLToPath(import.meta.url))

export function createSigilWindow(): BrowserWindow {
  const window = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 900,
    minHeight: 720,
    title: SIGIL_APP_NAME,
    backgroundColor: '#0d0d0e',
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      preload: path.join(currentDirectory, 'electron-preload.cjs')
    }
  })

  window.once('ready-to-show', () => window.show())
  const developmentServer = process.env.SIGIL_DEV_SERVER

  if (developmentServer) {
    void window.loadURL(developmentServer)
  } else {
    void window.loadFile(path.join(currentDirectory, 'index.html'))
  }

  return window
}

app.whenReady().then(() => {
  createSigilWindow()
  app.on('activate', () => {
    if (BrowserWindow.getAllWindows().length === 0) {createSigilWindow()}
  })
})

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') {app.quit()}
})
