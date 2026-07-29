import { useEffect, useRef, useState } from 'react'

const STORAGE_KEY = 'hermes-voice-selected-device-id'

export interface MicrophoneDevice {
  deviceId: string
  label: string
}

export function useMicDevice() {
  const [selectedDeviceId, setSelectedDeviceId] = useState<string | null>(() => {
    if (typeof window === 'undefined') {
      return null
    }

    return window.localStorage.getItem(STORAGE_KEY)
  })
  const [devices, setDevices] = useState<MicrophoneDevice[]>([])
  const [pendingDeviceId, setPendingDeviceId] = useState<string | null>(null)
  const initializedRef = useRef(false)

  const refreshDevices = async () => {
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.enumerateDevices) {
      return
    }

    try {
      const all = await navigator.mediaDevices.enumerateDevices()
      const inputs = all
        .filter((device): device is MediaDeviceInfo & { deviceId: string } => device.kind === 'audioinput')
        .map(device => ({
          deviceId: device.deviceId,
          label: device.label || `Microphone ${device.deviceId.slice(0, 6)}`
        }))

      setDevices(inputs)
    } catch {
      // enumeration is best-effort
    }
  }

  const chooseDevice = async (deviceId: string | null) => {
    if (!deviceId) {
      setPendingDeviceId(null)
      setSelectedDeviceId(null)
      if (typeof window !== 'undefined') {
        window.localStorage.removeItem(STORAGE_KEY)
      }
      return
    }

    try {
      await navigator.mediaDevices.getUserMedia({
        audio: { deviceId: { exact: deviceId } }
      })
    } catch {
      // keep previous choice if probing fails
    }

    setPendingDeviceId(deviceId)
    setSelectedDeviceId(deviceId)
    if (typeof window !== 'undefined') {
      window.localStorage.setItem(STORAGE_KEY, deviceId)
    }
  }

  useEffect(() => {
    if (typeof navigator === 'undefined' || !navigator.mediaDevices?.getUserMedia) {
      return
    }

    let cancelled = false

    async function prime() {
      try {
        await navigator.mediaDevices.getUserMedia({ audio: true })
      } catch {
        // non-fatal: labels may stay empty until permission is granted
      }

      if (cancelled) {
        return
      }

      await refreshDevices()

      if (cancelled) {
        return
      }

      const current = selectedDeviceId
      if (current && !devices.some(device => device.deviceId === current)) {
        await chooseDevice(devices[0]?.deviceId ?? null)
      }

      initializedRef.current = true
    }

    prime()

    return () => {
      cancelled = true
    }
  }, [])

  const clear = () => {
    setPendingDeviceId(null)
    setSelectedDeviceId(null)
    if (typeof window !== 'undefined') {
      window.localStorage.removeItem(STORAGE_KEY)
    }
  }

  return {
    clear,
    chooseDevice,
    devices,
    pendingDeviceId,
    refreshDevices,
    selectedDeviceId
  }
}
