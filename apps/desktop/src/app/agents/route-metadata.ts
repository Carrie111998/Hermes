export interface WorkerRouteMetadata {
  model?: string
  provider?: string
  reasoningEffort?: string
  workerProfile?: string
}

export function routeMetadataLabels(route: WorkerRouteMetadata): string[] {
  return [route.workerProfile, route.provider, route.model, route.reasoningEffort].filter(
    (label): label is string => !!label
  )
}
