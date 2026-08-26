import { registerPlugin } from '@capacitor/core'

interface InboundShareItem {
  id?: string
  mimeType?: string
  name?: string
}

interface InboundSharePayload {
  items?: InboundShareItem[]
  text?: string
}

interface InboundShareReadResult {
  base64?: string
  mimeType?: string
  name?: string
}

interface InboundSharePlugin {
  addListener(
    eventName: 'shareReceived',
    listener: (payload: InboundSharePayload) => void
  ): Promise<{ remove: () => void | Promise<void> }>
  getPending(): Promise<InboundSharePayload>
  readItem(options: { id: string }): Promise<InboundShareReadResult>
}

const InboundShare = registerPlugin<InboundSharePlugin>('InboundShare')

function decodeBase64(base64: string): Uint8Array | null {
  try {
    const binary = atob(base64)
    const bytes = new Uint8Array(binary.length)

    for (let index = 0; index < binary.length; index += 1) {
      bytes[index] = binary.charCodeAt(index)
    }

    return bytes
  } catch {
    return null
  }
}

/**
 * Consume only the files the user explicitly shared into Hermes. The Android
 * plugin retains URI grants only until a successful read, and JavaScript keeps
 * only browser File objects for the current app process.
 */
export async function consumePendingInboundShare(): Promise<{ files: File[]; text: string }> {
  let payload: InboundSharePayload

  try {
    payload = await InboundShare.getPending()
  } catch {
    return { files: [], text: '' }
  }

  const files: File[] = []
  for (const item of payload.items ?? []) {
    if (!item.id) continue

    try {
      const content = await InboundShare.readItem({ id: item.id })
      const bytes = content.base64 ? decodeBase64(content.base64) : null
      if (!bytes) continue

      const mimeType = content.mimeType || item.mimeType || 'application/octet-stream'
      const name = content.name || item.name || 'shared-file'
      const ownedBytes = new Uint8Array(bytes.byteLength)
      ownedBytes.set(bytes)
      files.push(new File([ownedBytes.buffer], name, { type: mimeType }))
    } catch {
      // An expired/malformed URI must not erase any companion share text.
    }
  }

  return { files, text: typeof payload.text === 'string' ? payload.text : '' }
}

/** Listen for shares delivered while Hermes is already open. */
export async function listenForInboundShare(handler: () => void): Promise<() => void> {
  try {
    const listener = await InboundShare.addListener('shareReceived', handler)
    return () => {
      void listener.remove()
    }
  } catch {
    return () => undefined
  }
}
