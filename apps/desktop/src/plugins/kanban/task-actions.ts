import {
  host,
  KANBAN_TASK_ACTIONS_AREA,
  type KanbanTaskActionContext,
  type KanbanTaskActionContribution,
  type KanbanTaskActionLocation,
  useContributions
} from '@hermes/plugin-sdk'

import type { KanbanTask } from './types'

export interface ResolvedKanbanTaskAction extends KanbanTaskActionContribution {
  id: string
}

export function resolveKanbanTaskActions(
  contributions: ReturnType<typeof useContributions>,
  location: KanbanTaskActionLocation
): ResolvedKanbanTaskAction[] {
  return contributions.flatMap(contribution => {
    const action = contribution.data as KanbanTaskActionContribution | null | undefined

    if (!action || typeof action.label !== 'string' || !action.label.trim() || typeof action.run !== 'function') {
      return []
    }

    if (action.locations && !action.locations.includes(location)) {
      return []
    }

    return [{ ...action, id: contribution.id, label: action.label.trim() }]
  })
}

export function useKanbanTaskActions(location: KanbanTaskActionLocation): ResolvedKanbanTaskAction[] {
  return resolveKanbanTaskActions(useContributions(KANBAN_TASK_ACTIONS_AREA), location)
}

export function taskActionContext(
  board: string,
  location: KanbanTaskActionLocation,
  task: KanbanTask
): KanbanTaskActionContext {
  return {
    board,
    location,
    task: {
      assignee: task.assignee,
      body: task.body,
      id: task.id,
      priority: task.priority,
      status: task.status,
      tenant: task.tenant,
      title: task.title
    }
  }
}

export async function runKanbanTaskAction(
  action: ResolvedKanbanTaskAction,
  context: KanbanTaskActionContext
): Promise<void> {
  try {
    await action.run(context)
  } catch (error) {
    host.notifyError(error, `Could not run ${action.label}`)
  }
}