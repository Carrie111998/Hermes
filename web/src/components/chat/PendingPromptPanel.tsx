import { Button } from "@nous-research/ui/ui/components/button";
import { AlertCircle, Check } from "lucide-react";
import { useState } from "react";

import type { WebChatPendingPrompt } from "@/components/chat/contracts";
import { Card } from "@nous-research/ui/ui/components/card";

interface PendingPromptPanelProps {
  pendingPrompt: WebChatPendingPrompt;
  setPendingPrompt: (prompt: WebChatPendingPrompt | null) => void;
  answerApproval: (choice: "once" | "session" | "deny") => Promise<void>;
  answerClarify: (answer: string) => Promise<void>;
  answerSudoOrSecret: () => Promise<void>;
}

export function PendingPromptPanel({
  pendingPrompt,
  setPendingPrompt,
  answerApproval,
  answerClarify,
  answerSudoOrSecret,
}: PendingPromptPanelProps) {
  const [clarifyAnswer, setClarifyAnswer] = useState("");

  return (
    <Card className="mt-3 border-[#e6cf8f]/25 bg-[#e6cf8f]/10 px-3 py-3 text-[#f4eddd]">
      <div className="mb-2 flex items-center gap-2 text-xs uppercase tracking-[0.18em] text-[#e6cf8f]">
        <AlertCircle className="h-3.5 w-3.5" />
        Input needed
      </div>

      {pendingPrompt.kind === "clarify" && (
        <div className="space-y-2">
          <div className="text-sm">{pendingPrompt.question}</div>
          {pendingPrompt.choices?.length ? (
            <div className="flex flex-wrap gap-2">
              {pendingPrompt.choices.map((choice) => (
                <Button key={choice} size="sm" onClick={() => void answerClarify(choice)}>
                  {choice}
                </Button>
              ))}
            </div>
          ) : null}
          <div className="flex gap-2">
            <input
              value={clarifyAnswer}
              onChange={(ev) => setClarifyAnswer(ev.target.value)}
              className="min-w-0 flex-1 rounded-md border border-current/20 bg-black/25 px-2 py-1.5 text-xs outline-none"
            />
            <Button
              size="sm"
              disabled={!clarifyAnswer.trim()}
              onClick={() => void answerClarify(clarifyAnswer.trim())}
            >
              <Check className="h-3.5 w-3.5" />
            </Button>
          </div>
        </div>
      )}

      {pendingPrompt.kind === "approval" && (
        <div className="space-y-2">
          <div className="text-sm">{pendingPrompt.description}</div>
          {pendingPrompt.command && (
            <pre className="max-h-32 overflow-auto whitespace-pre-wrap rounded-md bg-black/25 p-2 text-xs">
              {pendingPrompt.command}
            </pre>
          )}
          <div className="flex flex-wrap gap-2">
            <Button size="sm" onClick={() => void answerApproval("once")}>
              approve once
            </Button>
            <Button size="sm" outlined onClick={() => void answerApproval("session")}>
              approve session
            </Button>
            <Button size="sm" ghost onClick={() => void answerApproval("deny")}>
              deny
            </Button>
          </div>
        </div>
      )}

      {pendingPrompt.kind === "sudo" && (
        <div className="space-y-2">
          <div className="text-sm">sudo password required</div>
          <input
            value={pendingPrompt.password}
            type="password"
            onChange={(ev) =>
              setPendingPrompt({ ...pendingPrompt, password: ev.target.value })
            }
            className="w-full rounded-md border border-current/20 bg-black/25 px-2 py-1.5 text-xs outline-none"
          />
          <Button size="sm" onClick={() => void answerSudoOrSecret()}>
            submit
          </Button>
        </div>
      )}

      {pendingPrompt.kind === "secret" && (
        <div className="space-y-2">
          <div className="text-sm">{pendingPrompt.prompt}</div>
          <div className="text-xs text-[#e7e0d1]/55">{pendingPrompt.envVar}</div>
          <input
            value={pendingPrompt.value}
            type="password"
            onChange={(ev) =>
              setPendingPrompt({ ...pendingPrompt, value: ev.target.value })
            }
            className="w-full rounded-md border border-current/20 bg-black/25 px-2 py-1.5 text-xs outline-none"
          />
          <Button size="sm" onClick={() => void answerSudoOrSecret()}>
            submit
          </Button>
        </div>
      )}
    </Card>
  );
}
