import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

const desktopSrc = resolve(__dirname, '..')
const styles = () => readFileSync(resolve(desktopSrc, 'styles.css'), 'utf8')

describe('Atlas visual assets', () => {
  it('uses the approved optimized Atlas background from the desktop assets directory', () => {
    const asset = './assets/atlas-hermes-god-background-optimized.png'

    expect(styles()).toContain(`url('${asset}')`)
    expect(existsSync(resolve(desktopSrc, asset))).toBe(true)
    expect(styles()).not.toContain("url('/ds-assets/filler-bg0.jpg')")
  })
})
