import { describe, expect, it } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

import { LUNAR_CITY_ASSET_MANIFEST } from './world-assets'

describe('Lunar City asset manifest', () => {
  it('covers the full interactive world asset contract', () => {
    expect(LUNAR_CITY_ASSET_MANIFEST.schemaVersion).toBe(2)
    expect(LUNAR_CITY_ASSET_MANIFEST.glb).toBe('lunar-city/lunar-city-baseline.glb')
    expect(LUNAR_CITY_ASSET_MANIFEST.heroAssetGlb).toBe('lunar-city/hero-assets/lunar-city-hero-assets.glb')
    expect(LUNAR_CITY_ASSET_MANIFEST.heroAssetManifest).toBe('lunar-city/hero-assets/hero-assets-manifest.json')
    expect(LUNAR_CITY_ASSET_MANIFEST.heroAssetPreview).toBe('lunar-city/hero-assets/lunar-city-hero-assets.png')
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

  it('tracks the generated sculpted hero asset library', () => {
    const manifest = JSON.parse(
      readFileSync(join(process.cwd(), 'public/lunar-city/hero-assets/hero-assets-manifest.json'), 'utf8')
    ) as {
      assetCount: number
      buildings: Array<{ collection: string; id: string }>
      children: Array<{ collection: string; id: string }>
      heroMeshComponentCount: number
      leaders: Array<{ collection: string; id: string }>
      proceduralPbrMaterialCount: number
      proceduralPbrMaterials: string[]
      sculptedCharacterCoreComponentCount: number
      sculptedSurfaceComponentCount: number
      validation: Record<string, boolean>
      workers: Array<{ collection: string; id: string }>
    }

    expect(manifest.assetCount).toBe(26)
    expect(manifest.buildings).toHaveLength(8)
    expect(manifest.leaders).toHaveLength(8)
    expect(manifest.workers).toHaveLength(6)
    expect(manifest.children).toHaveLength(4)
    expect(manifest.heroMeshComponentCount).toBeGreaterThan(600)
    expect(manifest.sculptedSurfaceComponentCount).toBeGreaterThanOrEqual(76)
    expect(manifest.sculptedCharacterCoreComponentCount).toBeGreaterThanOrEqual(36)
    expect(manifest.proceduralPbrMaterialCount).toBeGreaterThanOrEqual(12)
    expect(manifest.proceduralPbrMaterials).toContain('Hero white hull PBR')
    expect(manifest.proceduralPbrMaterials).toContain('Hero leader fur')
    expect(manifest.validation.usesContinuousSculptedSurfaces).toBe(true)
    expect(manifest.validation.usesContinuousCharacterCoreMeshes).toBe(true)
    expect(manifest.validation.usesProceduralPbrMaterials).toBe(true)
    expect(manifest.validation.freeLocalGenerationOnly).toBe(true)
    expect(manifest.validation.noRawSoulContent).toBe(true)
    for (const asset of [...manifest.buildings, ...manifest.leaders, ...manifest.workers, ...manifest.children]) {
      expect(asset.collection).toBe(`Hero Asset - ${asset.id}`)
    }
  })
})
