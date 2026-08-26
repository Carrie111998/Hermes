import type { HermesSelectPathsOptions } from '@/global'

const MOBILE_FILE_PREFIX = 'mobile-file://'
const IMAGE_MIME_BY_EXTENSION: Record<string, string> = {
  '.bmp': 'image/bmp',
  '.gif': 'image/gif',
  '.jpeg': 'image/jpeg',
  '.jpg': 'image/jpeg',
  '.png': 'image/png',
  '.tif': 'image/tiff',
  '.tiff': 'image/tiff',
  '.webp': 'image/webp',
}

let nextFileId = 1
let filesByPath = new Map<string, File>()
let pathsByFile = new WeakMap<File, string>()

/**
 * Give a browser-selected file an opaque app-local handle. It never exposes an
 * Android filesystem path: byte reads remain limited to the exact File object
 * returned by the system picker/share chooser.
 */
export function mobilePathForFile(file: File): string {
  const existing = pathsByFile.get(file)
  if (existing) return existing

  const path = `${MOBILE_FILE_PREFIX}${nextFileId++}`
  pathsByFile.set(file, path)
  filesByPath.set(path, file)
  return path
}

/** Keep captured/pasted image bytes in the same process-local picker store. */
export function mobileImagePathForBytes(data: ArrayBuffer | Uint8Array, extension: string): string {
  const ext = extension.startsWith('.') ? extension.toLowerCase() : `.${extension.toLowerCase()}`
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data)
  const ownedBytes = new Uint8Array(bytes.byteLength)
  ownedBytes.set(bytes)
  const file = new File([ownedBytes.buffer], `image${ext}`, { type: IMAGE_MIME_BY_EXTENSION[ext] ?? 'image/png' })

  return mobilePathForFile(file)
}

export function isMobileFilePath(path: string): boolean {
  return path.startsWith(MOBILE_FILE_PREFIX)
}

function base64FromBytes(bytes: Uint8Array): string {
  let binary = ''
  const chunkSize = 0x8000

  for (let offset = 0; offset < bytes.length; offset += chunkSize) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize))
  }

  return btoa(binary)
}

/** Read only a file previously selected through the mobile picker/share path. */
export async function dataUrlForMobileFile(path: string): Promise<string> {
  const file = filesByPath.get(path)
  if (!file) return ''

  const content = base64FromBytes(new Uint8Array(await file.arrayBuffer()))
  const mime = file.type || 'application/octet-stream'
  return `data:${mime};base64,${content}`
}

export async function textForMobileFile(path: string): Promise<string> {
  const file = filesByPath.get(path)
  return file ? file.text() : ''
}

/** Convert Electron-style extension filters to a browser/Android picker accept list. */
export function mobileFilePickerAccept(options?: HermesSelectPathsOptions): string {
  const extensions = options?.filters?.flatMap(filter => filter.extensions) ?? []
  const normalized = [...new Set(extensions.map(ext => ext.trim().replace(/^\./, '').toLowerCase()).filter(Boolean))]
  return normalized.map(ext => `.${ext}`).join(',')
}

/**
 * Ask Android's system file/photo chooser for user-selected files. Folder
 * selection deliberately stays unavailable: the remote mobile client must not
 * request broad directory/storage access merely to mimic Desktop context refs.
 */
export function selectMobilePaths(options?: HermesSelectPathsOptions): Promise<string[]> {
  if (options?.directories || typeof document === 'undefined') return Promise.resolve([])

  return new Promise(resolve => {
    const input = document.createElement('input')
    input.type = 'file'
    input.multiple = options?.multiple !== false
    input.accept = mobileFilePickerAccept(options)
    input.style.display = 'none'

    const finish = (files: FileList | null) => {
      input.remove()
      resolve(Array.from(files ?? []).map(mobilePathForFile))
    }

    input.addEventListener('change', () => finish(input.files), { once: true })
    input.addEventListener('cancel', () => finish(null), { once: true })
    document.body.append(input)
    input.click()
  })
}

/** Test-only reset; production file handles live only for the app process. */
export function clearMobileFileStoreForTests(): void {
  nextFileId = 1
  filesByPath = new Map<string, File>()
  pathsByFile = new WeakMap<File, string>()
}
