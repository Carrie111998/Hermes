import { AlertCircle, Bot, Loader2, Paperclip, User, Wrench } from "lucide-react";
import { useEffect, useRef } from "react";

import type { WebChatMessage } from "@/components/chat/contracts";
import { cn } from "@/lib/utils";

export function ChatMessageList({ messages }: { messages: WebChatMessage[] }) {
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
  }, [messages]);

  return (
    <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-5 sm:px-6">
      {messages.length === 0 ? <EmptyChatState /> : <MessageStack messages={messages} />}
    </div>
  );
}

function EmptyChatState() {
  return (
    <div className="mx-auto flex min-h-full max-w-3xl flex-col justify-center py-12">
      <div className="mb-7 flex items-center gap-4">
        <div className="flex h-16 w-16 items-center justify-center rounded-xl border border-[#8ef7e0]/35 bg-[#8ef7e0]/10 shadow-[0_0_30px_rgba(142,247,224,0.16)]">
          <Bot className="h-8 w-8 text-[#9df8e8]" />
        </div>
        <h1 className="max-w-xl text-3xl font-semibold leading-tight tracking-normal text-[#f4eddd] sm:text-4xl">
          lif, what are we creating today?
        </h1>
      </div>
    </div>
  );
}

function MessageStack({ messages }: { messages: WebChatMessage[] }) {
  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      {messages.map((m) => (
        <div
          key={m.id}
          className={cn("flex gap-3", m.role === "user" ? "justify-end" : "justify-start")}
        >
          {m.role !== "user" && <MessageIcon role={m.role} />}
          <div
            className={cn(
              "min-w-0 max-w-[min(760px,86%)] rounded-md border px-4 py-3 text-sm leading-6 shadow-sm",
              m.role === "user"
                ? "border-[#8ef7e0]/25 bg-[#8ef7e0]/10 text-[#f5efe1]"
                : m.role === "assistant"
                  ? "border-white/10 bg-black/20 text-[#eee7d9]"
                  : "border-[#e6cf8f]/20 bg-[#e6cf8f]/10 text-[#f1dfaa]",
            )}
          >
            {m.attachments?.length ? (
              <div className="mb-2 flex flex-wrap gap-1.5">
                {m.attachments.map((a) => (
                  <span
                    key={a.id}
                    className="inline-flex max-w-full items-center gap-1 rounded-sm border border-current/20 px-2 py-1 text-xs text-[#e7e0d1]/70"
                  >
                    <Paperclip className="h-3 w-3 shrink-0" />
                    <span className="truncate">{a.name}</span>
                  </span>
                ))}
              </div>
            ) : null}
            <div className="whitespace-pre-wrap break-words">{m.text}</div>
            {m.status === "streaming" && (
              <Loader2 className="mt-2 h-3.5 w-3.5 animate-spin text-[#9df8e8]" />
            )}
          </div>
          {m.role === "user" && <MessageIcon role={m.role} />}
        </div>
      ))}
    </div>
  );
}

function MessageIcon({ role }: { role: WebChatMessage["role"] }) {
  return (
    <div
      className={cn(
        "mt-1 flex h-7 w-7 shrink-0 items-center justify-center rounded-md border",
        role === "user"
          ? "border-[#8ef7e0]/20 bg-[#8ef7e0]/10"
          : "border-current/15 bg-white/5",
      )}
    >
      {role === "assistant" ? (
        <Bot className="h-4 w-4 text-[#9df8e8]" />
      ) : role === "tool" ? (
        <Wrench className="h-4 w-4 text-[#e6cf8f]" />
      ) : role === "user" ? (
        <User className="h-4 w-4 text-[#9df8e8]" />
      ) : (
        <AlertCircle className="h-4 w-4 text-[#e7e0d1]/65" />
      )}
    </div>
  );
}
