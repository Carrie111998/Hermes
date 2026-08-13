import { $petInfo, petProfile, setPetInfo } from '@/store/pet'

export type GatewayRequest = <T>(
  method: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<T>

export const PET_SCALE_MIN = 0.1
export const PET_SCALE_MAX = 3.0
export const PET_SCALE_DEFAULT = 0.33
export const clampPetScale = (n: number) => Math.max(PET_SCALE_MIN, Math.min(PET_SCALE_MAX, n))

const WHEEL_SCALE_K = 0.0015

export function nextScaleFromWheel(current: number | undefined, deltaY: number): number {
  return clampPetScale((current ?? PET_SCALE_DEFAULT) * Math.exp(-deltaY * WHEEL_SCALE_K))
}

let scalePersist: ReturnType<typeof setTimeout> | undefined

export function setPetScale(request: GatewayRequest, scale: number): void {
  const next = clampPetScale(scale)
  setPetInfo({ ...$petInfo.get(), scale: next })
  clearTimeout(scalePersist)
  scalePersist = setTimeout(() => {
    request<{ ok: boolean; scale?: number }>('pet.scale', { profile: petProfile(), scale: next })
      .then(result => {
        if (typeof result?.scale === 'number' && result.scale !== $petInfo.get().scale) {
          setPetInfo({ ...$petInfo.get(), scale: result.scale })
        }
      })
      .catch(() => undefined)
  }, 200)
}
