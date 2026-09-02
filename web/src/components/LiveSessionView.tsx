/**
 * Read-only Live view for a gateway-owned (or any) session.
 *
 * Unlike `/chat?resume=<id>`, this path never opens a PTY / ui-tui runtime.
 * It polls persisted messages (and follows valid compression continuations)
 * so the dashboard can observe externally written turns without becoming a
 * second writer.
 */

import { Button } from "@nous-research/ui/ui/components/button";
import { Typography } from "@nous-research/ui/ui/components/typography/index";
import { Eye, Play, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router";

import { usePageHeader } from "@/contexts/usePageHeader";
import { useProfileScope } from "@/contexts/useProfileScope";
import { useI18n } from "@/i18n";
import { api, type SessionMessage } from "@/lib/api";
import { normalizeSessionTitle } from "@/lib/chat-title";
import { cn } from "@/lib/utils";

const LIVE_POLL_MS = 2500;

interface LiveSessionViewProps {
  sessionId: string;
  isActive: boolean;
}

export function LiveSessionView({ sessionId, isActive }: LiveSessionViewProps) {
  const { t } = useI18n();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const { setTitle } = usePageHeader();
  const { profile: scopedProfile } = useProfileScope();

  const [messages, setMessages] = useState<SessionMessage[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [title, setLocalTitle] = useState<string | null>(null);
  const [revision, setRevision] = useState<{
    messageCount: number | null;
    lastActive: number | null;
  }>({ messageCount: null, lastActive: null });
  const bottomRef = useRef<HTMLDivElement | null>(null);
  const targetIdRef = useRef(sessionId);

  useEffect(() => {
    targetIdRef.current = sessionId;
  }, [sessionId]);

  const liveViewLabel = t.sessions.liveView ?? "Live view";
  const readOnlyHint =
    t.sessions.liveViewReadOnlyHint ??
    "Read-only. New turns from the gateway appear here without starting a TUI.";
  const resumeLabel = t.sessions.resumeInChat ?? "Resume in Chat";

  const load = useCallback(
    async (silent = false) => {
      const id = targetIdRef.current;
      if (!silent) setLoading(true);
      try {
        // Follow compression continuations only — latest-descendant is already
        // constrained server-side to valid continuations (#98690 family).
        const descendant = await api.getSessionLatestDescendant(
          id,
          scopedProfile,
        );
        if (
          descendant.session_id &&
          descendant.session_id !== id &&
          descendant.changed
        ) {
          const next = new URLSearchParams(searchParams);
          next.set("live", descendant.session_id);
          setSearchParams(next, { replace: true });
          targetIdRef.current = descendant.session_id;
        }
        const liveId = targetIdRef.current;
        const [detail, msgs] = await Promise.all([
          api.getSessionDetail(liveId, scopedProfile),
          api.getSessionMessages(liveId, scopedProfile),
        ]);
        setLocalTitle(normalizeSessionTitle(detail.title));
        setRevision({
          messageCount: detail.message_count ?? null,
          lastActive: detail.last_active ?? null,
        });
        setMessages(msgs.messages);
        setError(null);
      } catch (err) {
        setError(String(err));
      } finally {
        if (!silent) setLoading(false);
      }
    },
    [scopedProfile, searchParams, setSearchParams],
  );

  useEffect(() => {
    void load(false);
  }, [sessionId, load]);

  useEffect(() => {
    if (!isActive) return;
    const timer = setInterval(() => {
      void load(true);
    }, LIVE_POLL_MS);
    return () => clearInterval(timer);
  }, [isActive, load]);

  useEffect(() => {
    if (!isActive) {
      setTitle(null);
      return;
    }
    setTitle(title ? `${liveViewLabel}: ${title}` : liveViewLabel);
    return () => setTitle(null);
  }, [isActive, title, liveViewLabel, setTitle]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages.length]);

  return (
    <div className="flex h-full min-h-0 flex-col bg-background">
      <div className="flex flex-wrap items-center gap-2 border-b border-border px-3 py-2">
        <Eye className="h-4 w-4 text-muted-foreground" aria-hidden />
        <Typography variant="sm" className="text-muted-foreground">
          {readOnlyHint}
        </Typography>
        <div className="ml-auto flex items-center gap-2">
          <Button
            ghost
            size="sm"
            onClick={() => void load(false)}
            aria-label="Refresh live view"
            title="Refresh"
          >
            <RefreshCw className="h-4 w-4" />
          </Button>
          <Button
            outlined
            size="sm"
            onClick={() =>
              navigate(`/chat?resume=${encodeURIComponent(sessionId)}`)
            }
            aria-label={resumeLabel}
            title={
              t.sessions.resumeWritableHint ??
              "Starts a writable TUI runtime for this session"
            }
          >
            <Play className="mr-1 h-4 w-4" />
            {resumeLabel}
          </Button>
        </div>
      </div>

      {error && (
        <div className="border-b border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
          {error}
        </div>
      )}

      <div className="min-h-0 flex-1 overflow-y-auto px-3 py-3">
        {loading && messages.length === 0 ? (
          <Typography variant="sm" className="text-muted-foreground">
            Loading…
          </Typography>
        ) : messages.length === 0 ? (
          <Typography variant="sm" className="text-muted-foreground">
            No messages yet.
          </Typography>
        ) : (
          <ul className="flex flex-col gap-3">
            {messages.map((m, idx) => (
              <li
                key={`${m.timestamp ?? idx}-${m.role}-${idx}`}
                className={cn(
                  "rounded border border-border px-3 py-2 text-sm whitespace-pre-wrap break-words",
                  m.role === "user" && "bg-primary/5",
                  m.role === "assistant" && "bg-background-base/40",
                  m.role === "tool" && "bg-muted/30 font-mono text-xs",
                  m.role === "system" && "text-muted-foreground",
                )}
              >
                <div className="mb-1 text-[11px] uppercase tracking-wide text-muted-foreground">
                  {m.role}
                  {revision.messageCount != null && idx === messages.length - 1
                    ? ` · ${revision.messageCount} msgs`
                    : null}
                </div>
                {m.content || ""}
              </li>
            ))}
            <div ref={bottomRef} />
          </ul>
        )}
      </div>
    </div>
  );
}

export default LiveSessionView;
