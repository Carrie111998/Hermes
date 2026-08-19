export function normalizeConnectionList(value) {
  if (Array.isArray(value)) {
    return value
  }

  if (Array.isArray(value?.connections)) {
    return value.connections
  }

  return []
}
