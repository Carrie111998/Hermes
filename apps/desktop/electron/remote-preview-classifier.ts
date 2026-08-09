const DEFAULT_PREVIEW_PORTS: Record<string, number> = {
  'http:': 80,
  'https:': 443
}

export interface RemotePreviewTargetClassification {
  isHttp: boolean
  isLocal: boolean
  remotePort: number | null
  url: URL
}

function normalizedHostname(hostname: string) {
  return hostname.replace(/^\[|\]$/g, '').toLowerCase().replace(/\.+$/, '')
}

function isNumericIpv4Loopback(hostname: string) {
  const octets = hostname.split('.')

  if (octets.length !== 4 || octets.some(octet => !/^\d{1,3}$/.test(octet))) {
    return false
  }

  const values = octets.map(Number)

  return values.every(value => value >= 0 && value <= 255) && values[0] === 127
}

function parseIpv6Groups(hostname: string): number[] | null {
  const parts = hostname.split('::')

  if (parts.length > 2) {
    return null
  }

  function parsePart(part: string) {
    if (!part) {
      return []
    }

    const values: number[] = []

    for (const group of part.split(':')) {
      if (group.includes('.')) {
        const octets = group.split('.')

        if (octets.length !== 4 || octets.some(octet => !/^\d{1,3}$/.test(octet))) {
          return null
        }

        const ipv4 = octets.map(Number)

        if (!ipv4.every(value => value >= 0 && value <= 255)) {
          return null
        }

        values.push((ipv4[0] << 8) | ipv4[1], (ipv4[2] << 8) | ipv4[3])
      } else {
        if (!/^[0-9a-f]{1,4}$/.test(group)) {
          return null
        }

        values.push(Number.parseInt(group, 16))
      }
    }

    return values
  }

  const left = parsePart(parts[0])
  const right = parts.length === 2 ? parsePart(parts[1]) : []

  if (!left || !right) {
    return null
  }

  if (parts.length === 1) {
    return left.length === 8 ? left : null
  }

  const compressed = 8 - left.length - right.length

  return compressed > 0 ? [...left, ...Array(compressed).fill(0), ...right] : null
}

function isNumericIpv6Loopback(hostname: string) {
  const groups = parseIpv6Groups(hostname)

  if (!groups) {
    return false
  }

  if (groups.every(group => group === 0)) {
    return true
  }

  if (groups.every((group, index) => (index === 7 ? group === 1 : group === 0))) {
    return true
  }

  return (
    groups.slice(0, 5).every(group => group === 0) &&
    groups[5] === 0xffff &&
    groups[6] >= 0x7f00 &&
    groups[6] <= 0x7fff
  )
}

function isLocalPreviewHostname(hostname: string) {
  const normalized = normalizedHostname(hostname)

  return (
    normalized === 'localhost' ||
    normalized === '0.0.0.0' ||
    isNumericIpv4Loopback(normalized) ||
    isNumericIpv6Loopback(normalized)
  )
}

export function classifyRemotePreviewTarget(rawTarget: string): RemotePreviewTargetClassification | null {
  let url: URL

  try {
    url = new URL(String(rawTarget || '').trim())
  } catch {
    return null
  }

  const isHttp = Object.hasOwn(DEFAULT_PREVIEW_PORTS, url.protocol)
  const remotePort = isHttp ? Number(url.port || DEFAULT_PREVIEW_PORTS[url.protocol]) : null

  return {
    isHttp,
    isLocal: isHttp && isLocalPreviewHostname(url.hostname),
    remotePort:
      remotePort !== null && Number.isInteger(remotePort) && remotePort >= 1 && remotePort <= 65535
        ? remotePort
        : null,
    url
  }
}
