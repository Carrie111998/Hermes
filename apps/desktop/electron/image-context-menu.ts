export function imageContextMenuItems(params, actions) {
  if (params.mediaType !== 'image' || !params.hasImageContents) {
    return []
  }

  const items = []
  const srcURL = params.srcURL || ''

  if (actions.removeAttachment) {
    items.push({
      label: 'Remove Attachment',
      click: actions.removeAttachment
    })
  }

  const mediaItems = []

  if (srcURL) {
    mediaItems.push({
      label: 'Open Image',
      click: () => {
        if (!srcURL.startsWith('data:')) {
          actions.openImage(srcURL)
        }
      },
      enabled: !srcURL.startsWith('data:')
    })
  }

  mediaItems.push({
    label: 'Copy Image',
    click: () => actions.copyImageAt(params.x, params.y)
  })

  if (srcURL) {
    mediaItems.push(
      {
        label: 'Copy Image Address',
        click: () => actions.copyImageAddress(srcURL)
      },
      {
        label: 'Save Image As...',
        click: () => actions.saveImage(srcURL)
      }
    )
  }

  if (items.length && mediaItems.length) {
    items.push({ type: 'separator' })
  }

  items.push(...mediaItems)

  return items
}

export function composerAttachmentIdAtPointScript(x, y) {
  const left = Number(x)
  const top = Number(y)

  if (!Number.isFinite(left) || !Number.isFinite(top)) {
    return 'null'
  }

  return `(() => {
    const el = document.elementFromPoint(${left}, ${top})
    return el?.closest('[data-composer-attachment-id]')?.getAttribute('data-composer-attachment-id') || null
  })()`
}

export function clickComposerAttachmentRemoveScript(id) {
  const selector = `[data-composer-attachment-id=${JSON.stringify(id)}] [data-slot="composer-attachment-remove"]`

  return `document.querySelector(${JSON.stringify(selector)})?.click()`
}

export async function resolveComposerAttachmentRemove(webContents, params, isDestroyed = () => false) {
  if (params.mediaType !== 'image' || isDestroyed()) {
    return undefined
  }

  let attachmentId

  try {
    attachmentId = await webContents.executeJavaScript(composerAttachmentIdAtPointScript(params.x, params.y))
  } catch {
    return undefined
  }

  if (!attachmentId || typeof attachmentId !== 'string') {
    return undefined
  }

  return () => {
    if (isDestroyed()) {
      return
    }

    void webContents.executeJavaScript(clickComposerAttachmentRemoveScript(attachmentId)).catch(() => undefined)
  }
}
