import { describe, expect, it } from 'vitest'

import { LUNAR_CITY_ASSET_MANIFEST } from './world-assets'

describe('Lunar City asset manifest', () => {
  it('covers the full interactive world asset contract', () => {
    expect(LUNAR_CITY_ASSET_MANIFEST.schemaVersion).toBe(2)
    expect(LUNAR_CITY_ASSET_MANIFEST.glb).toBe('lunar-city/lunar-city-baseline.glb')
    expect(LUNAR_CITY_ASSET_MANIFEST.profileManifest).toBe('lunar-city/profile-assets.json')
    expect(LUNAR_CITY_ASSET_MANIFEST.assets.filter(asset => asset.kind === 'building')).toHaveLength(8)
    expect(LUNAR_CITY_ASSET_MANIFEST.assets.filter(asset => asset.kind === 'character')).toHaveLength(19)
    expect(LUNAR_CITY_ASSET_MANIFEST.assets.some(asset => asset.id === 'terrain-colony-basin')).toBe(true)
    expect(LUNAR_CITY_ASSET_MANIFEST.assets.some(asset => asset.id === 'road-network-primary')).toBe(true)
    expect(LUNAR_CITY_ASSET_MANIFEST.assets.some(asset => asset.id === 'dispatcher-cube')).toBe(true)
    expect(LUNAR_CITY_ASSET_MANIFEST.animationClips.map(clip => clip.clip)).toEqual([
      'idle',
      'walk',
      'work',
      'carry',
      'inspect',
      'repair',
      'talk',
      'wait',
      'panic',
      'celebrate',
      'rest',
      'return'
    ])
    expect(LUNAR_CITY_ASSET_MANIFEST.validation.requires).toContain('buildings_do_not_overlap')
  })

  it('maps every role visual identity to a home building or scene object', () => {
    const buildings = new Set(
      LUNAR_CITY_ASSET_MANIFEST.assets.filter(asset => asset.kind === 'building' || asset.kind === 'prop').map(asset => asset.id)
    )

    for (const asset of LUNAR_CITY_ASSET_MANIFEST.assets.filter(asset => asset.kind === 'character')) {
      for (const binding of asset.bindings ?? []) {
        if (binding.homeBuilding) {
          expect(buildings.has(binding.homeBuilding)).toBe(true)
        }
      }
    }
  })

  it('declares texture slots only at approved 2k or 4k resolutions', () => {
    expect(LUNAR_CITY_ASSET_MANIFEST.textures.length).toBeGreaterThan(8)

    for (const texture of LUNAR_CITY_ASSET_MANIFEST.textures) {
      expect(['2k', '4k']).toContain(texture.maxResolution)

      for (const slot of texture.slots) {
        expect(slot.uri).toContain(`lunar-city/textures/${texture.id}/`)
        expect(['2k', '4k']).toContain(slot.resolution)
      }
    }
  })
})
