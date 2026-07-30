import type { PtyConnectionState } from "@/lib/pty-reconnect";

export const OPEN_SOCKET_READY_STATE = 1;

export interface ChatLearnSeedPlan {
  command: string;
  nextSearchParams: URLSearchParams;
}

interface PlanChatLearnSeedInput {
  isActive: boolean;
  ptyState: PtyConnectionState;
  socketReadyState: number | null;
  searchParams: URLSearchParams;
}

export function planChatLearnSeed({
  isActive,
  ptyState,
  socketReadyState,
  searchParams,
}: PlanChatLearnSeedInput): ChatLearnSeedPlan | null {
  const learnSeed = searchParams.get("learn");

  if (
    !isActive ||
    ptyState !== "open" ||
    socketReadyState !== OPEN_SOCKET_READY_STATE ||
    !learnSeed
  ) {
    return null;
  }

  const nextSearchParams = new URLSearchParams(searchParams);
  nextSearchParams.delete("learn");

  return {
    command: `/learn ${learnSeed}`.trim() + "\r",
    nextSearchParams,
  };
}
