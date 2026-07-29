import { Badge } from "@nous-research/ui/ui/components/badge";
import { Circle, Wrench } from "lucide-react";

import { PendingPromptPanel } from "@/components/chat/PendingPromptPanel";
import type { WebChatPendingPrompt, WebChatSessionInfo } from "@/components/chat/contracts";
import { ToolCall, type ToolEntry } from "@/components/ToolCall";
import { Card } from "@nous-research/ui/ui/components/card";
import type { ConnectionState } from "@/lib/gatewayClient";
import { cn } from "@/lib/utils";

export const STATE_LABEL: Record<ConnectionState, string> = {
  idle: "idle",
  connecting: "connecting",
  open: "live",
  closed: "closed",
  error: "error",
};

export const STATE_TONE: Record<
  ConnectionState,
  "secondary" | "warning" | "success" | "destructive"
> = {
  idle: "secondary",
  connecting: "warning",
  open: "success",
  closed: "secondary",
  error: "destructive",
};

interface ChatInspectorProps {
  answerApproval: (choice: "once" | "session" | "deny") => Promise<void>;
  answerClarify: (answer: string) => Promise<void>;
  answerSudoOrSecret: () => Promise<void>;
  currentStatus: string;
  error: string | null;
  info: WebChatSessionInfo;
  pendingPrompt: WebChatPendingPrompt | null;
  sessionId: string | null;
  setPendingPrompt: (prompt: WebChatPendingPrompt | null) => void;
  state: ConnectionState;
  tools: ToolEntry[];
}

export function displayModel(model?: string): string {
  return (model ?? "model").split("/").filter(Boolean).pop() ?? "model";
}

export function ChatInspector({
  answerApproval,
  answerClarify,
  answerSudoOrSecret,
  currentStatus,
  error,
  info,
  pendingPrompt,
  sessionId,
  setPendingPrompt,
  state,
  tools,
}: ChatInspectorProps) {
  const modelLabel = displayModel(info.model);
  const statusText = error ?? info.credential_warning ?? currentStatus;

  return (
    <aside className="hidden w-80 shrink-0 flex-col border-l border-current/10 bg-black/16 p-3 lg:flex">
      <Card className="border-white/10 bg-white/[0.035] px-3 py-3 text-[#e7e0d1]">
        <div className="mb-2 flex items-center justify-between gap-2">
          <div className="text-xs uppercase tracking-[0.18em] text-[#e7e0d1]/45">
            Session
          </div>
          <Badge tone={STATE_TONE[state]}>{STATE_LABEL[state]}</Badge>
        </div>
        <div className="truncate text-sm font-medium">{modelLabel}</div>
        <div className="mt-1 truncate text-xs text-[#e7e0d1]/45">
          {sessionId ?? "no session"}
        </div>
        <div className="mt-2 flex items-start gap-2 text-xs text-[#e7e0d1]/55">
          <Circle className="mt-1 h-2.5 w-2.5 shrink-0 text-[#9df8e8]" />
          <span className="min-w-0 break-words">{statusText}</span>
        </div>
      </Card>

      {pendingPrompt && (
        <PendingPromptPanel
          pendingPrompt={pendingPrompt}
          setPendingPrompt={setPendingPrompt}
          answerApproval={answerApproval}
          answerClarify={answerClarify}
          answerSudoOrSecret={answerSudoOrSecret}
        />
      )}

      <Card className="mt-3 flex min-h-0 flex-1 flex-col border-white/10 bg-white/[0.035] px-2 py-2 text-[#e7e0d1]">
        <div className="flex items-center gap-2 px-1 pb-2 text-xs uppercase tracking-[0.18em] text-[#e7e0d1]/45">
          <Wrench className="h-3.5 w-3.5" />
          Tools
        </div>
        <div className="min-h-0 overflow-y-auto">
          {tools.length === 0 ? (
            <div className="px-2 py-5 text-center text-xs text-[#e7e0d1]/45">
              no tool calls yet
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {tools.map((tool) => (
                <div key={tool.id} className="rounded-md border border-white/10 bg-black/20">
                  <div className="flex items-center justify-between gap-2 px-2 py-1.5 text-xs">
                    <span className="min-w-0 truncate">{tool.name}</span>
                    <span
                      className={cn(
                        "shrink-0",
                        tool.status === "error"
                          ? "text-red-200"
                          : tool.status === "running"
                            ? "text-[#e6cf8f]"
                            : "text-[#9df8e8]",
                      )}
                    >
                      {tool.status === "running" ? "running" : tool.status === "error" ? "error" : "done"}
                    </span>
                  </div>
                  <ToolCall tool={tool} />
                </div>
              ))}
            </div>
          )}
        </div>
      </Card>
    </aside>
  );
}
