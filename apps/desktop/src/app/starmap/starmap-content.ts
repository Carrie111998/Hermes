interface StarmapContentShape {
  candidates?: readonly unknown[]
  nodes: readonly unknown[]
}

export function hasStarmapContent(graph: StarmapContentShape) {
  return graph.nodes.length > 0 || (graph.candidates?.length ?? 0) > 0
}
