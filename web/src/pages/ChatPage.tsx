import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { AlertCircle, Bot, RefreshCw, Square } from "lucide-react";
import { useEffect } from "react";
import { useSearchParams } from "react-router-dom";

import { ChatComposer } from "@/components/chat/ChatComposer";
import {
  ChatInspector,
  STATE_LABEL,
  STATE_TONE,
  displayModel,
} from "@/components/chat/ChatInspector";
import { ChatMessageList } from "@/components/chat/ChatMessageList";
import { usePageHeader } from "@/contexts/usePageHeader";
import { useGatewayChat } from "@/hooks/useGatewayChat";
import { cn } from "@/lib/utils";
import { PluginSlot } from "@/plugins";

export default function ChatPage({ isActive = true }: { isActive?: boolean }) {
  const [searchParams] = useSearchParams();
  const { setEnd } = usePageHeader();
  const chat = useGatewayChat(searchParams.get("resume"));
  const canSubmit = chat.state === "open" && !!chat.sessionId && !chat.running;
  const modelLabel = displayModel(chat.info.model);

  useEffect(() => {
    if (isActive) setEnd(null);
  }, [isActive, setEnd]);

  return (
    <div className="relative flex min-h-0 flex-1 flex-col overflow-hidden">
      <PluginSlot name="chat:top" />

      <div
        className={cn(
          "relative flex min-h-0 flex-1 flex-col overflow-hidden",
          "border border-current/15 bg-[#151615] text-[#e7e0d1]",
          "shadow-[0_20px_80px_rgba(0,0,0,0.35)]",
        )}
      >
        <div
          aria-hidden
          className="pointer-events-none absolute inset-0 opacity-[0.22]"
          style={{
            backgroundImage:
              "radial-gradient(circle at 1px 1px, rgba(231,224,209,0.26) 1px, transparent 0)",
            backgroundSize: "18px 18px",
          }}
        />
        <div className="pointer-events-none absolute inset-x-0 top-0 h-24 bg-[linear-gradient(180deg,rgba(122,247,222,0.10),transparent)]" />

        <div className="relative z-1 flex min-h-0 flex-1">
          <main className="flex min-w-0 flex-1 flex-col">
            <header className="flex h-14 shrink-0 items-center justify-between gap-3 border-b border-current/10 px-4 sm:px-6">
              <div className="flex min-w-0 items-center gap-3">
                <div className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md border border-[#8ef7e0]/35 bg-[#8ef7e0]/10">
                  <Bot className="h-4 w-4 text-[#9df8e8]" />
                </div>
                <div className="min-w-0">
                  <div className="truncate text-sm font-semibold tracking-wide">
                    Hermes Agent
                  </div>
                  <div className="truncate text-xs text-[#e7e0d1]/55">
                    {chat.info.provider ?? "gateway"} · {modelLabel}
                  </div>
                </div>
              </div>

              <div className="flex shrink-0 items-center gap-2">
                <Badge tone={STATE_TONE[chat.state]}>{STATE_LABEL[chat.state]}</Badge>
                {chat.running && (
                  <Button
                    ghost
                    size="sm"
                    onClick={() => void chat.interrupt()}
                    className="rounded-md border border-current/20 px-2 py-1 text-xs normal-case tracking-normal"
                  >
                    <Square className="h-3 w-3" />
                    stop
                  </Button>
                )}
                <Button
                  ghost
                  size="icon"
                  onClick={chat.reconnect}
                  aria-label="Reconnect"
                  className="rounded-md border border-current/20 text-[#e7e0d1]/70 hover:text-[#9df8e8]"
                >
                  <RefreshCw className="h-4 w-4" />
                </Button>
              </div>
            </header>

            {chat.error && (
              <div className="relative z-1 mx-4 mt-3 flex items-start gap-2 border border-red-400/30 bg-red-950/30 px-3 py-2 text-xs text-red-200 sm:mx-6">
                <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                <span className="min-w-0 break-words">{chat.error}</span>
              </div>
            )}

            <ChatMessageList messages={chat.messages} />

            <ChatComposer
              attachments={chat.attachments}
              attachmentPath={chat.attachmentPath}
              attachError={chat.attachError}
              uploadingAttachment={chat.uploadingAttachment}
              canSubmit={canSubmit}
              input={chat.input}
              sessionId={chat.sessionId}
              setAttachmentPath={chat.setAttachmentPath}
              setInput={chat.setInput}
              attachLocalPath={() => void chat.attachLocalPath()}
              uploadAttachment={(file) => void chat.uploadAttachment(file)}
              removeAttachment={chat.removeAttachment}
              sendCurrentInput={() => void chat.sendCurrentInput()}
            />
          </main>

          <ChatInspector
            answerApproval={chat.answerApproval}
            answerClarify={chat.answerClarify}
            answerSudoOrSecret={chat.answerSudoOrSecret}
            currentStatus={chat.currentStatus}
            error={chat.error}
            info={chat.info}
            pendingPrompt={chat.pendingPrompt}
            sessionId={chat.sessionId}
            setPendingPrompt={chat.setPendingPrompt}
            state={chat.state}
            tools={chat.tools}
          />
        </div>
      </div>

      <PluginSlot name="chat:bottom" />
    </div>
  );
}
