import type { SessionInfo } from "./api";

export interface SessionDeleteTarget {
  id: string;
  profile: string;
}

/** Preserve the row owner across the delete-confirmation round trip. */
export function sessionDeleteTarget(session: Pick<SessionInfo, "id" | "profile">): SessionDeleteTarget {
  return { id: session.id, profile: session.profile };
}
