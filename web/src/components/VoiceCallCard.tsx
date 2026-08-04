/**
 * Realtime voice call card (dashboard sidebar).
 *
 * Holds the xAI S2S socket directly in the browser (ephemeral token minted by
 * the backend via `voice.realtime_token`; browser AEC → full duplex on
 * speakers). grok-voice converses instantly and delegates real work to Hermes
 * through consult/steer tool calls, which run as turns in a dedicated
 * dashboard voice session on this card's own JSON-RPC sidecar — deliberately
 * NOT the embedded TUI's session, so the two surfaces never fight over one
 * transport. Works from a phone browser too — that's the mobile story.
 */

import { RealtimeVoiceClient, type RealtimeTokenGrant } from "@hermes/shared";
import { Mic, MicOff, Square } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";

import { useI18n } from "@/i18n";
import { GatewayClient } from "@/lib/gatewayClient";
import { cn } from "@/lib/utils";

type CallStatus = "idle" | "connecting" | "listening" | "speaking" | "thinking";

interface TranscriptLine {
  id: number;
  who: "assistant" | "hermes";
  text: string;
}

const MAX_TRANSCRIPT_LINES = 8;
const MAX_CONSULT_OUTPUT_CHARS = 6000;

export function VoiceCallCard({ profile }: { profile?: string | null }) {
  const { t } = useI18n();
  const [status, setStatus] = useState<CallStatus>("idle");
  const [muted, setMuted] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [transcript, setTranscript] = useState<TranscriptLine[]>([]);
  const gwRef = useRef<GatewayClient | null>(null);
  const clientRef = useRef<RealtimeVoiceClient | null>(null);
  const consultRef = useRef<{ callId: string; task: string } | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const lineIdRef = useRef(0);

  const pushLine = useCallback((who: TranscriptLine["who"], text: string) => {
    setTranscript((prev) =>
      [...prev, { id: ++lineIdRef.current, who, text }].slice(-MAX_TRANSCRIPT_LINES),
    );
  }, []);

  const stop = useCallback(() => {
    clientRef.current?.close();
    clientRef.current = null;
    gwRef.current?.close();
    gwRef.current = null;
    consultRef.current = null;
    sessionIdRef.current = null;
    setStatus("idle");
    setMuted(false);
  }, []);

  useEffect(() => stop, [stop]);

  const start = useCallback(async () => {
    if (clientRef.current) return;
    setError(null);
    setTranscript([]);
    setStatus("connecting");
    try {
      const gw = new GatewayClient();
      gwRef.current = gw;
      await gw.connect();
      const grant = await gw.request<RealtimeTokenGrant>("voice.realtime_token", {});
      const sessionId = `voice-web-${Date.now().toString(36)}`;
      sessionIdRef.current = sessionId;

      // Consult turns complete via the terminal message.complete event on
      // this card's own sidecar/session.
      gw.on("message.complete", (ev) => {
        const consult = consultRef.current;
        const client = clientRef.current;
        if (!consult || !client || ev.session_id !== sessionIdRef.current) return;
        consultRef.current = null;
        const payload = (ev.payload ?? {}) as { text?: string };
        let output = String(payload.text ?? "").trim() || "Hermes finished with no text output.";
        if (output.length > MAX_CONSULT_OUTPUT_CHARS) {
          output = `${output.slice(0, MAX_CONSULT_OUTPUT_CHARS)}\n[truncated]`;
        }
        pushLine("hermes", output);
        client.sendFunctionOutput(consult.callId, output);
      });

      const client = new RealtimeVoiceClient();
      clientRef.current = client;
      await client.connect(grant, {
        onFunctionCall: (call) => {
          const active = clientRef.current;
          if (!active) return;
          if (call.name === "consult_hermes") {
            const task = String(call.args.task ?? "").trim();
            if (!task) {
              active.sendFunctionOutput(call.callId, "No task provided.");
              return;
            }
            if (consultRef.current) {
              active.sendFunctionOutput(
                call.callId,
                "Hermes is still working on the previous task; its result will arrive shortly.",
              );
              return;
            }
            consultRef.current = { callId: call.callId, task };
            if (!active.lastResponseHadAudio) active.speakAcknowledgment();
            void gw.request("prompt.submit", {
              session_id: sessionId,
              text: task,
              ...(profile ? { profile } : {}),
            });
            return;
          }
          if (call.name === "steer_hermes") {
            const instruction = String(call.args.instruction ?? "").trim();
            if (!instruction || !consultRef.current) {
              active.sendFunctionOutput(
                call.callId,
                instruction
                  ? "No Hermes task is running — use consult_hermes to start one."
                  : "No steering instruction provided.",
              );
              return;
            }
            consultRef.current = { callId: consultRef.current.callId, task: instruction };
            void gw.request("prompt.submit", { session_id: sessionId, text: instruction });
            void gw.request("session.interrupt", { session_id: sessionId }).catch(() => undefined);
            active.sendFunctionOutput(call.callId, "Steering applied — Hermes is adjusting course.");
            return;
          }
          active.sendFunctionOutput(call.callId, `Unknown tool: ${call.name}`);
        },
        onAssistantTranscript: (text) => pushLine("assistant", text),
        onUserTranscript: (text) => {
          if (text.trim().toLowerCase().replace(/[.!]/g, "") === "stop") stop();
        },
        onStatus: (clientStatus, detail) => {
          if (!clientRef.current) return;
          if (clientStatus === "speaking") setStatus("speaking");
          else if (clientStatus === "listening") {
            setStatus(consultRef.current ? "thinking" : "listening");
          } else if (clientStatus === "error") {
            setError(detail ?? "realtime error");
          } else if (clientStatus === "closed") {
            stop();
          }
        },
      });
      setStatus("listening");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      stop();
    }
  }, [profile, pushLine, stop]);

  const toggleMute = useCallback(() => {
    setMuted((prev) => {
      const next = !prev;
      clientRef.current?.setMuted(next);
      return next;
    });
  }, []);

  const live = status !== "idle";
  const statusLabel =
    status === "connecting"
      ? t.voiceCall.connecting
      : status === "thinking"
        ? t.voiceCall.working
        : status === "speaking"
          ? t.voiceCall.speaking
          : status === "listening"
            ? t.voiceCall.listening
            : t.voiceCall.title;

  return (
    <div className="px-2 py-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => (live ? stop() : void start())}
          className={cn(
            "flex items-center gap-1.5 rounded border px-2 py-1 text-xs transition-colors",
            live
              ? "border-red-500/50 text-red-400 hover:bg-red-500/10"
              : "border-current/20 hover:bg-current/10",
          )}
          title={live ? t.voiceCall.end : t.voiceCall.start}
        >
          {live ? <Square className="h-3 w-3" /> : <Mic className="h-3 w-3" />}
          <span>{live ? t.voiceCall.end : t.voiceCall.start}</span>
        </button>
        {live && (
          <button
            type="button"
            onClick={toggleMute}
            className="rounded border border-current/20 p-1 hover:bg-current/10"
            title={muted ? t.voiceCall.unmute : t.voiceCall.mute}
          >
            {muted ? <MicOff className="h-3 w-3" /> : <Mic className="h-3 w-3" />}
          </button>
        )}
        <span
          className={cn(
            "ml-auto text-[0.65rem] uppercase tracking-wide opacity-70",
            status === "speaking" && "text-emerald-400",
            status === "thinking" && "text-amber-400",
          )}
        >
          {statusLabel}
        </span>
      </div>
      {error && <div className="mt-1 text-xs text-red-400">{error}</div>}
      {live && transcript.length > 0 && (
        <ul className="mt-2 space-y-1 text-xs opacity-80">
          {transcript.map((line) => (
            <li key={line.id} className="truncate">
              <span className="opacity-60">{line.who === "assistant" ? "🎙" : "⚕"}</span>{" "}
              {line.text}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
