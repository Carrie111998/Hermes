export type DrawerTab = 'execution' | 'overview' | 'timeline'

type DrawerTabStatsInput = {
  comments: number
  events: number
  hasLog: boolean
  running: boolean
  runs: number
}

export function drawerTabStats({ comments, events, hasLog, running, runs }: DrawerTabStatsInput) {
  return {
    executionCount: runs || (hasLog ? 1 : 0),
    executionLive: running,
    timelineCount: comments + events
  }
}
