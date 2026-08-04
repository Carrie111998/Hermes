import type { AuthMeResponse } from "@/lib/api";

/** True only for the least-privilege identity minted by a phone handoff. */
export function isResumeOnlySession(me: AuthMeResponse): boolean {
  return (
    me.scopes.length === 1 &&
    me.scopes[0] === "resume" &&
    me.bound_session_id.trim().length > 0
  );
}
