import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import type { ToolEntry } from "@/components/ToolCall";
import {
  type WebChatAttachment,
  type WebChatDetectDropResult,
  type WebChatImageAttachResult,
  type WebChatMessage,
  type WebChatPendingPrompt,
  type WebChatSessionCreateResult,
  type WebChatSessionInfo,
  type WebChatSessionResumeResult,
} from "@/components/chat/contracts";
import {
  GatewayClient,
  type ConnectionState,
  type GatewayEvent,
} from "@/lib/gatewayClient";
import { uploadChatAttachment } from "@/lib/chatUpload";
import { executeSlash } from "@/lib/slashExec";

const TOOL_LIMIT = 24;
let seq = 0;

function nextId(prefix: string): string {
  seq += 1;
  return `${prefix}-${Date.now().toString(36)}-${seq}`;
}

function basename(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).pop() || path;
}

function attachmentMeta(r: WebChatImageAttachResult): string | undefined {
  const bits: string[] = [];
  if (r.width && r.height) bits.push(`${r.width}x${r.height}`);
  if (r.token_estimate) bits.push(`~${r.token_estimate} tokens`);
  if (r.count) bits.push(`image #${r.count}`);
  return bits.length ? bits.join(" · ") : undefined;
}

function historyMessages(
  items: WebChatSessionResumeResult["messages"],
): WebChatMessage[] {
  if (!items?.length) return [];
  return items
    .map((m): WebChatMessage | null => {
      const role =
        m.role === "assistant" || m.role === "user" || m.role === "system"
          ? m.role
          : m.role === "tool"
            ? "tool"
            : null;
      if (!role) return null;
      const text =
        role === "tool" ? `${m.name ?? "tool"} ${m.context ?? ""}`.trim() : m.text ?? "";
      if (!text.trim()) return null;
      return { id: nextId(`history-${role}`), role, text, status: "complete" };
    })
    .filter((m): m is WebChatMessage => m !== null);
}

export function useGatewayChat(resumeSessionId: string | null) {
  const [version, setVersion] = useState(0);
  const gw = useMemo(() => new GatewayClient(), [version]);
  const activeAssistantRef = useRef<string | null>(null);
  const sessionIdRef = useRef<string | null>(null);
  const [state, setState] = useState<ConnectionState>("idle");
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [info, setInfo] = useState<WebChatSessionInfo>({});
  const [messages, setMessages] = useState<WebChatMessage[]>([]);
  const [tools, setTools] = useState<ToolEntry[]>([]);
  const [currentStatus, setCurrentStatus] = useState("connecting");
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [pendingPrompt, setPendingPrompt] = useState<WebChatPendingPrompt | null>(null);
  const [attachments, setAttachments] = useState<WebChatAttachment[]>([]);
  const [attachmentPath, setAttachmentPath] = useState("");
  const [attachError, setAttachError] = useState<string | null>(null);
  const [uploadingAttachment, setUploadingAttachment] = useState(false);
  const [input, setInput] = useState("");

  const addSystem = useCallback((text: string) => {
    if (!text.trim()) return;
    setMessages((prev) => [
      ...prev,
      { id: nextId("system"), role: "system", text, status: "complete" },
    ]);
  }, []);

  const beginAssistant = useCallback(() => {
    const id = nextId("assistant");
    activeAssistantRef.current = id;
    setMessages((prev) => [
      ...prev,
      { id, role: "assistant", text: "", status: "streaming" },
    ]);
    return id;
  }, []);

  const appendAssistantDelta = useCallback(
    (text: string) => {
      if (!text) return;
      const id = activeAssistantRef.current ?? beginAssistant();
      setMessages((prev) =>
        prev.map((m) => (m.id === id ? { ...m, text: m.text + text } : m)),
      );
    },
    [beginAssistant],
  );

  const completeAssistant = useCallback((text?: string) => {
    const id = activeAssistantRef.current;
    if (!id) {
      if (text?.trim()) {
        setMessages((prev) => [
          ...prev,
          { id: nextId("assistant"), role: "assistant", text, status: "complete" },
        ]);
      }
      setRunning(false);
      return;
    }
    setMessages((prev) =>
      prev.map((m) =>
        m.id === id
          ? { ...m, text: text?.trim() ? text : m.text, status: "complete" }
          : m,
      ),
    );
    activeAssistantRef.current = null;
    setRunning(false);
    setCurrentStatus("idle");
  }, []);

  const handleEvent = useCallback(
    (ev: GatewayEvent) => {
      switch (ev.type) {
        case "session.info":
          if (ev.session_id) {
            sessionIdRef.current = ev.session_id;
            setSessionId(ev.session_id);
          }
          if (ev.payload && typeof ev.payload === "object") {
            setInfo((prev) => ({ ...prev, ...(ev.payload as WebChatSessionInfo) }));
          }
          return;
        case "message.start":
          setRunning(true);
          beginAssistant();
          return;
        case "message.delta":
          appendAssistantDelta(String((ev.payload as { text?: string })?.text ?? ""));
          return;
        case "message.complete":
          completeAssistant(String((ev.payload as { text?: string })?.text ?? ""));
          return;
        case "status.update": {
          const p = ev.payload as { text?: string; kind?: string } | undefined;
          setCurrentStatus(p?.text || p?.kind || "running");
          return;
        }
        case "tool.start": {
          const p = ev.payload as
            | { tool_id?: string; name?: string; context?: string }
            | undefined;
          if (!p?.tool_id) return;
          const toolId = p.tool_id;
          setTools((prev) =>
            [
              ...prev,
              {
                kind: "tool" as const,
                id: `tool-${toolId}-${Date.now()}`,
                tool_id: toolId,
                name: p.name ?? "tool",
                context: p.context,
                status: "running" as const,
                startedAt: Date.now(),
              },
            ].slice(-TOOL_LIMIT),
          );
          return;
        }
        case "tool.progress": {
          const p = ev.payload as
            | { tool_id?: string; name?: string; preview?: string }
            | undefined;
          if (!p?.preview) return;
          setTools((prev) =>
            prev.map((t) =>
              (p.tool_id && t.tool_id === p.tool_id) ||
              (!p.tool_id && p.name && t.name === p.name && t.status === "running")
                ? { ...t, preview: p.preview }
                : t,
            ),
          );
          return;
        }
        case "tool.complete": {
          const p = ev.payload as
            | { tool_id?: string; summary?: string; error?: string; inline_diff?: string }
            | undefined;
          if (!p?.tool_id) return;
          setTools((prev) =>
            prev.map((t) =>
              t.tool_id === p.tool_id
                ? {
                    ...t,
                    status: p.error ? "error" : "done",
                    summary: p.summary,
                    error: p.error,
                    inline_diff: p.inline_diff,
                    completedAt: Date.now(),
                  }
                : t,
            ),
          );
          return;
        }
        case "clarify.request": {
          const p = ev.payload as
            | { request_id?: string; question?: string; choices?: string[] }
            | undefined;
          setPendingPrompt({
            kind: "clarify",
            requestId: p?.request_id ?? "",
            question: p?.question ?? "Clarification needed",
            choices: p?.choices,
          });
          setRunning(true);
          return;
        }
        case "approval.request": {
          const p = ev.payload as { command?: string; description?: string } | undefined;
          setPendingPrompt({
            kind: "approval",
            command: p?.command ?? "",
            description: p?.description ?? "approval needed",
          });
          setRunning(true);
          return;
        }
        case "sudo.request": {
          const p = ev.payload as { request_id?: string } | undefined;
          setPendingPrompt({ kind: "sudo", requestId: p?.request_id ?? "", password: "" });
          setRunning(true);
          return;
        }
        case "secret.request": {
          const p = ev.payload as
            | { request_id?: string; env_var?: string; prompt?: string }
            | undefined;
          setPendingPrompt({
            kind: "secret",
            requestId: p?.request_id ?? "",
            envVar: p?.env_var ?? "secret",
            prompt: p?.prompt ?? "Secret required",
            value: "",
          });
          setRunning(true);
          return;
        }
        case "error": {
          const message = String((ev.payload as { message?: string })?.message ?? "gateway error");
          setError(message);
          addSystem(`error: ${message}`);
          setRunning(false);
        }
      }
    },
    [addSystem, appendAssistantDelta, beginAssistant, completeAssistant],
  );

  useEffect(() => {
    let cancelled = false;
    const offState = gw.onState(setState);
    const offAny = gw.onAny((ev) => handleEvent(ev));

    async function start() {
      try {
        setError(null);
        setCurrentStatus("connecting");
        await gw.connect();
        if (cancelled) return;
        const created = resumeSessionId
          ? await gw.request<WebChatSessionResumeResult>("session.resume", {
              session_id: resumeSessionId,
            })
          : await gw.request<WebChatSessionCreateResult>("session.create", {});
        if (cancelled) return;
        sessionIdRef.current = created.session_id;
        setSessionId(created.session_id);
        if (created.info) setInfo(created.info);
        if (resumeSessionId) {
          setMessages(historyMessages((created as WebChatSessionResumeResult).messages));
        }
        setCurrentStatus("idle");
      } catch (e) {
        if (!cancelled) {
          const message = e instanceof Error ? e.message : String(e);
          setError(message);
          setCurrentStatus("connection failed");
        }
      }
    }

    void start();
    return () => {
      cancelled = true;
      offState();
      offAny();
      gw.close();
    };
  }, [gw, handleEvent, resumeSessionId]);

  const attachPath = useCallback(async (rawPath: string) => {
    const sid = sessionIdRef.current;
    const raw = rawPath.trim();
    if (!sid || !raw) return;
    setAttachError(null);
    try {
      const detected = await gw.request<WebChatDetectDropResult>("input.detect_drop", {
        session_id: sid,
        text: raw,
      });
      let result: WebChatDetectDropResult = detected;
      if (!detected.matched) {
        result = await gw.request<WebChatImageAttachResult>("image.attach", {
          session_id: sid,
          path: raw,
        });
      }
      const path = result.path ?? raw;
      const name = result.name ?? basename(path);
      const text = result.text ?? `[User attached image: ${name}]`;
      setAttachments((prev) => [
        ...prev,
        {
          id: nextId("attach"),
          kind: result.is_image === false ? "file" : "image",
          name,
          path,
          text,
          meta: attachmentMeta(result),
        },
      ]);
      setAttachmentPath("");
    } catch (e) {
      setAttachError(e instanceof Error ? e.message : String(e));
    }
  }, [gw]);

  const attachLocalPath = useCallback(async () => {
    await attachPath(attachmentPath);
  }, [attachPath, attachmentPath]);

  const uploadAttachment = useCallback(async (file: File) => {
    if (!sessionIdRef.current) return;
    setAttachError(null);
    setUploadingAttachment(true);
    try {
      const uploaded = await uploadChatAttachment(file);
      await attachPath(uploaded.path);
    } catch (e) {
      setAttachError(e instanceof Error ? e.message : String(e));
    } finally {
      setUploadingAttachment(false);
    }
  }, [attachPath]);

  const removeAttachment = useCallback((id: string) => {
    setAttachments((prev) => prev.filter((a) => a.id !== id));
  }, []);

  const submitPrompt = useCallback(
    async (text: string, includeAttachments = true) => {
      const sid = sessionIdRef.current;
      const trimmed = text.trim();
      if (!sid || !trimmed) return;
      const outboundAttachments = includeAttachments ? attachments : [];
      const prefix = outboundAttachments.map((a) => a.text).filter(Boolean).join("\n");
      const outbound = prefix ? `${prefix}\n\n${trimmed}` : trimmed;
      setMessages((prev) => [
        ...prev,
        {
          id: nextId("user"),
          role: "user",
          text: trimmed,
          attachments: outboundAttachments.length ? [...outboundAttachments] : undefined,
          status: "complete",
        },
      ]);
      if (includeAttachments) setAttachments([]);
      setRunning(true);
      setCurrentStatus("running");
      try {
        await gw.request("prompt.submit", { session_id: sid, text: outbound });
      } catch (e) {
        const message = e instanceof Error ? e.message : String(e);
        setError(message);
        setRunning(false);
        addSystem(`error: ${message}`);
      }
    },
    [addSystem, attachments, gw],
  );

  const sendCurrentInput = useCallback(async () => {
    const text = input.trim();
    if (!text || state !== "open" || !sessionId || running) return;
    setInput("");
    if (text.startsWith("/")) {
      await executeSlash({
        command: text,
        sessionId,
        gw,
        callbacks: { sys: addSystem, send: (message) => submitPrompt(message, false) },
      });
      return;
    }
    await submitPrompt(text);
  }, [addSystem, gw, input, running, sessionId, state, submitPrompt]);

  const interrupt = useCallback(async () => {
    const sid = sessionIdRef.current;
    if (!sid) return;
    await gw.request("session.interrupt", { session_id: sid }, 15_000);
    setRunning(false);
    setPendingPrompt(null);
    setCurrentStatus("interrupted");
  }, [gw]);

  const reconnect = useCallback(() => {
    setError(null);
    setTools([]);
    setPendingPrompt(null);
    activeAssistantRef.current = null;
    sessionIdRef.current = null;
    setSessionId(null);
    setVersion((v) => v + 1);
  }, []);

  const answerClarify = useCallback(
    async (answer: string) => {
      if (!pendingPrompt || pendingPrompt.kind !== "clarify") return;
      await gw.request("clarify.respond", { request_id: pendingPrompt.requestId, answer });
      setMessages((prev) => [
        ...prev,
        { id: nextId("clarify-answer"), role: "user", text: answer, status: "complete" },
      ]);
      setPendingPrompt(null);
      setCurrentStatus("running");
    },
    [gw, pendingPrompt],
  );

  const answerApproval = useCallback(
    async (choice: "once" | "session" | "deny") => {
      if (!sessionIdRef.current) return;
      await gw.request("approval.respond", {
        session_id: sessionIdRef.current,
        choice,
        all: choice === "session",
      });
      setPendingPrompt(null);
      setCurrentStatus(choice === "deny" ? "denied" : "running");
    },
    [gw],
  );

  const answerSudoOrSecret = useCallback(async () => {
    if (!pendingPrompt) return;
    if (pendingPrompt.kind === "sudo") {
      await gw.request("sudo.respond", {
        request_id: pendingPrompt.requestId,
        password: pendingPrompt.password,
      });
    } else if (pendingPrompt.kind === "secret") {
      await gw.request("secret.respond", {
        request_id: pendingPrompt.requestId,
        value: pendingPrompt.value,
      });
    }
    setPendingPrompt(null);
    setCurrentStatus("running");
  }, [gw, pendingPrompt]);

  return {
    state,
    sessionId,
    info,
    messages,
    tools,
    currentStatus,
    running,
    error,
    pendingPrompt,
    setPendingPrompt,
    attachments,
    attachmentPath,
    setAttachmentPath,
    attachError,
    uploadingAttachment,
    input,
    setInput,
    attachLocalPath,
    uploadAttachment,
    removeAttachment,
    sendCurrentInput,
    interrupt,
    reconnect,
    answerClarify,
    answerApproval,
    answerSudoOrSecret,
  };
}
