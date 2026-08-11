/** Crew + workflow data model, mirrored from plugins/crews/dashboard/plugin_api.py. */

export type CrewMemberStatus = 'idle' | 'running' | 'done' | 'error'
export type CrewStatus = 'draft' | 'active' | 'paused' | 'complete'
export type WorkflowTaskStatus = 'idle' | 'running' | 'done' | 'error'

export interface Persona {
  id: string
  name: string
  role: string
  emoji: string
  color: string
  specialties: string[]
}

export interface CrewTemplate {
  id: string
  name: string
  category: string
  goal: string
  members: Array<{ persona: string }>
}

export interface CrewMember {
  id: string
  persona: string
  displayName: string
  roleLabel: string
  color: string
  role: string
  model: string | null
  profileName: string | null
  status: CrewMemberStatus
  lastActivity: string | null
}

export interface Crew {
  id: string
  name: string
  goal: string
  status: CrewStatus
  createdAt: number
  updatedAt: number
  members: CrewMember[]
}

export interface WorkflowTask {
  id: string
  label: string
  prompt: string
  assigneeId: string | null
  x: number
  y: number
  status?: WorkflowTaskStatus
  lastActivity?: string | null
}

export interface WorkflowEdge {
  from: string
  to: string
}

export interface Workflow {
  id: string
  crewId: string
  tasks: WorkflowTask[]
  edges: WorkflowEdge[]
  createdAt: number
  updatedAt: number
}

export interface WorkflowRun {
  id: string
  crewId: string
  status: 'running' | 'complete' | 'error'
  tasks: Record<string, WorkflowTaskStatus>
  startedAt: number
  finishedAt: number | null
}

/** Live event from the /events socket. */
export type CrewEvent =
  | { type: 'crew_updated'; crewId: string; ts: string }
  | { type: 'crew_deleted'; crewId: string; ts: string }
  | { type: 'member_status'; crewId: string; memberId: string; status: CrewMemberStatus; detail?: string; ts: string }
  | {
      type: 'task_status'
      crewId: string
      runId?: string | null
      taskId: string
      status: WorkflowTaskStatus
      detail?: string
      ts: string
    }
  | {
      type: 'worker_end'
      crewId: string
      memberId?: string | null
      taskId?: string | null
      status: CrewMemberStatus | WorkflowTaskStatus
      detail?: string
      activity?: string
      ts: string
    }
  | { type: 'activity'; crewId: string; memberId?: string | null; taskId?: string | null; text: string; ts: string }
  | { type: 'run_started'; crewId: string; runId: string; ts: string }
  | { type: 'run_end'; crewId: string; runId: string; status: string; ts: string }
