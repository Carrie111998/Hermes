import { afterEach, describe, expect, it } from 'vitest'

import { clearMobileFileStoreForTests, dataUrlForMobileFile, mobileFilePickerAccept, mobilePathForFile } from './mobile-files'

afterEach(() => {
  clearMobileFileStoreForTests()
})

describe('mobile file bridge', () => {
  it('keeps a user-selected file behind an opaque local handle and reads its bytes as a data URL', async () => {
    const file = new File(['Fold notes'], 'notes.txt', { type: 'text/plain' })

    const path = mobilePathForFile(file)

    expect(path).toMatch(/^mobile-file:\/\//)
    expect(path).not.toContain('notes.txt')
    await expect(dataUrlForMobileFile(path)).resolves.toBe('data:text/plain;base64,Rm9sZCBub3Rlcw==')
  })

  it('returns the same opaque handle when the same browser File is attached twice', () => {
    const file = new File(['image'], 'photo.jpg', { type: 'image/jpeg' })

    expect(mobilePathForFile(file)).toBe(mobilePathForFile(file))
  })

  it('limits the Android picker to the caller-requested extensions', () => {
    expect(mobileFilePickerAccept({ filters: [{ extensions: ['png', 'jpg'], name: 'Images' }] })).toBe('.png,.jpg')
    expect(mobileFilePickerAccept()).toBe('')
  })
})
