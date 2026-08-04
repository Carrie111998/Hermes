import type { GroupRoom } from './group-model'

export type GroupRequester = (method: string, params?: Record<string, unknown>) => Promise<unknown>

export interface CreateGroupRoomInput {
  name: string
  profiles: string[]
  workspace?: string
  triggerTokens: number
  maxHistoryTokens: number
  tailMessageCount: number
}
export interface GroupRoomPageInput { beforeSeq?: number; cursor?: string }
export interface GroupRoomListResponse { rooms?: GroupRoom[] }
export interface GroupRoomResponse { room?: GroupRoom; cursor?: string | null; has_more?: boolean }

export function createGroupTransport(request: GroupRequester) {
  return {
    listRooms: () => request('group.room.list') as Promise<GroupRoomListResponse>,
    createRoom: (input: CreateGroupRoomInput) => request('group.room.create', {
      name: input.name,
      profiles: input.profiles,
      ...(input.workspace ? { workspace: input.workspace } : {}),
      trigger_tokens: input.triggerTokens,
      max_history_tokens: input.maxHistoryTokens,
      tail_message_count: input.tailMessageCount
    }) as Promise<GroupRoomResponse>,
    getRoom: (roomId: string, page?: GroupRoomPageInput) => request('group.room.get', {
      room_id: roomId,
      ...(page?.beforeSeq !== undefined ? { before_seq: page.beforeSeq } : {}),
      ...(page?.cursor ? { cursor: page.cursor } : {})
    }) as Promise<GroupRoomResponse>,
    deleteRoom: (roomId: string) => request('group.room.delete', { room_id: roomId }),
    sendMessage: (roomId: string, content: string, mentions: string[]) =>
      request('group.message.send', { room_id: roomId, content, mentions }),
    rewindMessage: (roomId: string, seq: number, content: string) =>
      request('group.message.rewind', { room_id: roomId, seq, content }) as Promise<GroupRoomResponse>,
    interrupt: (roomId: string, profile?: string) =>
      request('group.run.interrupt', { room_id: roomId, ...(profile ? { profile } : {}) }),
    subscribe: (roomId: string) => request('group.subscribe', { room_id: roomId }),
    unsubscribe: (roomId: string) => request('group.unsubscribe', { room_id: roomId }),
    respondToApproval: (runtimeSessionId: string, choice: 'once' | 'session' | 'always' | 'deny') =>
      request('approval.respond', { choice, session_id: runtimeSessionId }),
    respondToClarify: (requestId: string, answer: string) =>
      request('clarify.respond', { request_id: requestId, answer })
  }
}

export function mentionsFromText(content: string, profiles: readonly string[]): string[] {
  const found = new Set<string>()

  for (const match of content.matchAll(/(^|\s)@([\w.-]+)/g)) {
    const mention = match[2]

    if (mention === 'all' || profiles.includes(mention)) {found.add(mention)}
  }

  return [...found]
}
