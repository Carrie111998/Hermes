import {
  Bot,
  Check,
  ChevronDown,
  CircleStop,
  Clipboard,
  Code2,
  File,
  Link2,
  Mic,
  Paperclip,
  Plus,
  Send,
  Settings2,
  Sparkles,
  Trash2,
  Unlink,
  X,
} from "lucide-react";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
} from "react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { Input } from "@nous-research/ui/ui/components/input";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { Markdown } from "@/components/Markdown";
import { useProfileScope } from "@/contexts/useProfileScope";
import { api } from "@/lib/api";
import type {
  AgentHubAttachment,
  AgentHubConversation,
  AgentHubConversationSummary,
  AgentHubDiscordBinding,
  AgentHubDiscordChannel,
  AgentHubHarness,
  AgentHubResponse,
  SkillInfo,
} from "@/lib/api";
import { cn } from "@/lib/utils";

type HubMode = "chat" | "discord";

function draftId(): string {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return crypto.randomUUID();
  }
  return `hub-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function harnessIcon(id: AgentHubHarness["id"]) {
  if (id === "codex") return Code2;
  if (id === "claude") return Sparkles;
  return Bot;
}

function formatTime(epoch: number): string {
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(new Date(epoch * 1000));
}

function toDataUrl(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result || ""));
    reader.onerror = () => reject(reader.error);
    reader.readAsDataURL(blob);
  });
}

interface SkillsMenuProps {
  skills: SkillInfo[];
  selected: string[];
  onChange: (skills: string[]) => void;
  compact?: boolean;
}

function SkillsMenu({ skills, selected, onChange, compact = false }: SkillsMenuProps) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const selectedSet = useMemo(() => new Set(selected), [selected]);
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return skills
      .filter((skill) => skill.enabled)
      .filter(
        (skill) =>
          !needle ||
          skill.name.toLowerCase().includes(needle) ||
          skill.description.toLowerCase().includes(needle),
      )
      .sort((a, b) => a.name.localeCompare(b.name));
  }, [query, skills]);

  return (
    <div className="relative">
      <Button
        type="button"
        ghost
        size="sm"
        className={cn(
          "border border-border text-text-secondary hover:text-foreground",
          compact ? "h-8" : "h-9",
        )}
        onClick={() => setOpen((value) => !value)}
        prefix={<Sparkles className="h-3.5 w-3.5" />}
      >
        {selected.length ? `${selected.length} skills` : "Skills"}
        <ChevronDown className="ml-1 h-3.5 w-3.5" />
      </Button>
      {open && (
        <>
          <button
            type="button"
            aria-label="Close skills"
            className="fixed inset-0 z-30 cursor-default"
            onClick={() => setOpen(false)}
          />
          <Card className="absolute bottom-[calc(100%+0.5rem)] left-0 z-40 w-[min(22rem,calc(100vw-2rem))] border-border bg-background-base shadow-2xl">
            <CardContent className="p-3">
              <Input
                autoFocus
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Find a skill…"
                className="mb-2"
              />
              <div className="max-h-64 space-y-1 overflow-y-auto pr-1">
                {filtered.map((skill) => {
                  const checked = selectedSet.has(skill.name);
                  return (
                    <label
                      key={skill.name}
                      className="flex cursor-pointer items-start gap-2 rounded-sm px-2 py-2 hover:bg-foreground/5"
                    >
                      <Checkbox
                        checked={checked}
                        onCheckedChange={(value) =>
                          onChange(
                            value === true
                              ? [...selected, skill.name]
                              : selected.filter((name) => name !== skill.name),
                          )
                        }
                      />
                      <span className="min-w-0">
                        <span className="block text-sm text-foreground">{skill.name}</span>
                        <span className="line-clamp-2 text-xs text-muted-foreground">
                          {skill.description}
                        </span>
                      </span>
                    </label>
                  );
                })}
                {!filtered.length && (
                  <p className="px-2 py-6 text-center text-xs text-muted-foreground">
                    No enabled skills found.
                  </p>
                )}
              </div>
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}

export default function AgentHubPage() {
  const { profile } = useProfileScope();
  const { toast, showToast } = useToast();
  const [mode, setMode] = useState<HubMode>("chat");
  const [hub, setHub] = useState<AgentHubResponse | null>(null);
  const [skills, setSkills] = useState<SkillInfo[]>([]);
  const [channels, setChannels] = useState<AgentHubDiscordChannel[]>([]);
  const [loading, setLoading] = useState(true);
  const [conversation, setConversation] = useState<AgentHubConversation | null>(null);
  const [conversationDraftId, setConversationDraftId] = useState(draftId);
  const [harness, setHarness] = useState<AgentHubHarness["id"]>("codex");
  const [selectedSkills, setSelectedSkills] = useState<string[]>([]);
  const [cwd, setCwd] = useState("");
  const [model, setModel] = useState("");
  const [prompt, setPrompt] = useState("");
  const [attachments, setAttachments] = useState<AgentHubAttachment[]>([]);
  const [sending, setSending] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [recording, setRecording] = useState(false);
  const [transcribing, setTranscribing] = useState(false);
  const [bindingChannel, setBindingChannel] = useState("");
  const [bindingHarness, setBindingHarness] =
    useState<AgentHubHarness["id"]>("codex");
  const [bindingSkills, setBindingSkills] = useState<string[]>([]);
  const [bindingCwd, setBindingCwd] = useState("");
  const [savingBinding, setSavingBinding] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const audioChunksRef = useRef<Blob[]>([]);
  const transcriptEndRef = useRef<HTMLDivElement | null>(null);

  const reloadHub = useCallback(async () => {
    const [nextHub, nextSkills, channelResult] = await Promise.all([
      api.getAgentHub(profile || undefined),
      api.getSkills(profile || undefined),
      api.getAgentHubDiscordChannels(),
    ]);
    setHub(nextHub);
    setSkills(nextSkills);
    setChannels(channelResult.channels);
    setCwd((value) => value || nextHub.default_cwd);
    setBindingCwd((value) => value || nextHub.default_cwd);
    const preferred = nextHub.harnesses.find((item) => item.available);
    if (preferred) {
      setHarness((value) =>
        nextHub.harnesses.some((item) => item.id === value && item.available)
          ? value
          : preferred.id,
      );
      setBindingHarness((value) =>
        nextHub.harnesses.some((item) => item.id === value && item.available)
          ? value
          : preferred.id,
      );
    }
  }, [profile]);

  useEffect(() => {
    let cancelled = false;
    Promise.resolve()
      .then(reloadHub)
      .catch((error) => !cancelled && showToast(`Could not load Agent Hub: ${error}`, "error"))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [reloadHub, showToast]);

  useEffect(() => {
    transcriptEndRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [conversation?.messages.length, sending]);

  const availableHarnesses = hub?.harnesses ?? [];
  const currentHarness = availableHarnesses.find((item) => item.id === harness);
  const selectedChannel = channels.find((channel) => channel.id === bindingChannel);

  const newConversation = () => {
    setConversation(null);
    setConversationDraftId(draftId());
    setPrompt("");
    setAttachments([]);
    setSelectedSkills([]);
    setModel("");
    if (hub) setCwd(hub.default_cwd);
  };

  const openConversation = async (summary: AgentHubConversationSummary) => {
    try {
      const detail = await api.getAgentHubConversation(summary.id);
      setConversation(detail);
      setConversationDraftId(detail.id);
      setHarness(detail.harness);
      setSelectedSkills(detail.skills);
      setCwd(detail.cwd);
      setModel(detail.model || "");
      setAttachments([]);
    } catch (error) {
      showToast(`Could not open session: ${error}`, "error");
    }
  };

  const deleteConversation = async (id: string) => {
    try {
      await api.deleteAgentHubConversation(id);
      if (conversation?.id === id) newConversation();
      await reloadHub();
    } catch (error) {
      showToast(`Could not delete session: ${error}`, "error");
    }
  };

  const submit = async () => {
    const text = prompt.trim();
    if (!text || sending || !currentHarness?.available) return;
    const optimistic: AgentHubConversation = conversation ?? {
      id: conversationDraftId,
      title: text.split("\n")[0].slice(0, 72),
      harness,
      native_session_id: null,
      cwd,
      skills: selectedSkills,
      model,
      created_at: Date.now() / 1000,
      updated_at: Date.now() / 1000,
      messages: [],
    };
    setConversation({
      ...optimistic,
      messages: [
        ...optimistic.messages,
        {
          role: "user",
          content: text,
          attachments: attachments.map((item) => item.path),
          created_at: Date.now() / 1000,
        },
      ],
    });
    setPrompt("");
    setSending(true);
    try {
      const result = await api.runAgentHubTurn({
        harness,
        prompt: text,
        conversation_id: conversation?.id || conversationDraftId,
        cwd,
        skills: selectedSkills,
        attachments: attachments.map((item) => item.path),
        model: model.trim() || undefined,
        profile: profile || undefined,
      });
      setConversation(result);
      setAttachments([]);
      await reloadHub();
    } catch (error) {
      setPrompt(text);
      setConversation(conversation);
      showToast(`Coding agent failed: ${error}`, "error");
    } finally {
      setSending(false);
    }
  };

  const onPromptKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      void submit();
    }
  };

  const uploadFiles = async (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (!files.length) return;
    setUploading(true);
    try {
      const uploaded = await Promise.all(
        files.map((file) => api.uploadAgentHubFile(file, conversation?.id || conversationDraftId)),
      );
      setAttachments((current) => [...current, ...uploaded]);
    } catch (error) {
      showToast(`Upload failed: ${error}`, "error");
    } finally {
      setUploading(false);
    }
  };

  const stopRecording = () => mediaRecorderRef.current?.stop();

  const toggleRecording = async () => {
    if (recording) {
      stopRecording();
      return;
    }
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      showToast("Voice recording is not supported in this browser.", "error");
      return;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const recorder = new MediaRecorder(stream);
      audioChunksRef.current = [];
      recorder.ondataavailable = (event) => {
        if (event.data.size) audioChunksRef.current.push(event.data);
      };
      recorder.onstop = () => {
        stream.getTracks().forEach((track) => track.stop());
        setRecording(false);
        const blob = new Blob(audioChunksRef.current, {
          type: recorder.mimeType || "audio/webm",
        });
        setTranscribing(true);
        void toDataUrl(blob)
          .then((dataUrl) => api.transcribeAudio(dataUrl, blob.type))
          .then((result) =>
            setPrompt((current) =>
              [current.trim(), result.transcript].filter(Boolean).join(current.trim() ? " " : ""),
            ),
          )
          .catch((error) => showToast(`Transcription failed: ${error}`, "error"))
          .finally(() => setTranscribing(false));
      };
      mediaRecorderRef.current = recorder;
      recorder.start();
      setRecording(true);
    } catch (error) {
      showToast(`Microphone unavailable: ${error}`, "error");
    }
  };

  const saveBinding = async () => {
    if (!selectedChannel) return;
    setSavingBinding(true);
    try {
      await api.setAgentHubDiscordBinding({
        channel_id: selectedChannel.id,
        channel_name: [selectedChannel.guild, `#${selectedChannel.name}`]
          .filter(Boolean)
          .join(" / "),
        harness: bindingHarness,
        skills: bindingSkills,
        cwd: bindingCwd,
      });
      showToast("Discord channel bound to coding agent.", "success");
      setBindingChannel("");
      setBindingSkills([]);
      await reloadHub();
    } catch (error) {
      showToast(`Could not save binding: ${error}`, "error");
    } finally {
      setSavingBinding(false);
    }
  };

  const removeBinding = async (binding: AgentHubDiscordBinding) => {
    try {
      await api.deleteAgentHubDiscordBinding(binding.channel_id);
      await reloadHub();
      showToast("Discord binding removed.", "success");
    } catch (error) {
      showToast(`Could not remove binding: ${error}`, "error");
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-4">
      <Toast toast={toast} />
      <div className="flex flex-col gap-3 sm:flex-row sm:items-end sm:justify-between">
        <div>
          <div className="mb-1 flex items-center gap-2">
            <Code2 className="h-5 w-5 text-primary" />
            <h1 className="font-mondwest text-display text-xl tracking-wide">Agent Hub</h1>
          </div>
          <p className="max-w-2xl text-sm text-muted-foreground">
            Work directly with native coding agents, combine skills, and carry files into the session.
          </p>
        </div>
        <div className="flex rounded-sm border border-border bg-background/30 p-1">
          <Button
            size="sm"
            ghost={mode !== "chat"}
            onClick={() => setMode("chat")}
            prefix={<Bot className="h-4 w-4" />}
          >
            Direct chat
          </Button>
          <Button
            size="sm"
            ghost={mode !== "discord"}
            onClick={() => setMode("discord")}
            prefix={<Link2 className="h-4 w-4" />}
          >
            Discord binding
          </Button>
        </div>
      </div>

      {mode === "chat" ? (
        <div className="grid min-h-[38rem] flex-1 gap-3 lg:grid-cols-[16rem_minmax(0,1fr)]">
          <Card className="min-h-0 border-border bg-background/25">
            <CardContent className="flex h-full max-h-[72dvh] flex-col p-3">
              <Button
                className="mb-3 w-full uppercase"
                size="sm"
                onClick={newConversation}
                prefix={<Plus className="h-4 w-4" />}
              >
                New session
              </Button>
              <p className="mb-2 px-1 font-mondwest text-xs uppercase tracking-wider text-muted-foreground">
                Recent
              </p>
              <div className="flex gap-2 overflow-x-auto lg:flex-1 lg:flex-col lg:overflow-y-auto">
                {(hub?.conversations ?? []).map((item) => (
                  <div
                    key={item.id}
                    className={cn(
                      "group relative min-w-56 rounded-sm border p-3 text-left transition-colors lg:min-w-0",
                      conversation?.id === item.id
                        ? "border-primary/50 bg-primary/10"
                        : "border-transparent hover:border-border hover:bg-foreground/5",
                    )}
                  >
                    <button type="button" className="w-full pr-6 text-left" onClick={() => void openConversation(item)}>
                      <span className="block truncate text-sm font-medium text-foreground">
                        {item.title}
                      </span>
                      <span className="mt-1 block truncate text-xs text-muted-foreground">
                        {item.harness === "claude" ? "Claude Code" : item.harness} · {formatTime(item.updated_at)}
                      </span>
                    </button>
                    <Button
                      ghost
                      size="icon"
                      aria-label={`Delete ${item.title}`}
                      className="absolute right-1 top-1 h-7 w-7 opacity-0 group-hover:opacity-100"
                      onClick={() => void deleteConversation(item.id)}
                    >
                      <Trash2 className="h-3.5 w-3.5" />
                    </Button>
                  </div>
                ))}
                {!hub?.conversations.length && (
                  <p className="px-2 py-8 text-center text-xs text-muted-foreground">
                    Your coding sessions will appear here.
                  </p>
                )}
              </div>
            </CardContent>
          </Card>

          <Card className="min-h-0 overflow-hidden border-border bg-background/20">
            <CardContent className="flex h-full min-h-[38rem] flex-col p-0">
              <div className="border-b border-border p-3 sm:p-4">
                <div className="grid gap-2 sm:grid-cols-3">
                  {availableHarnesses.map((item) => {
                    const Icon = harnessIcon(item.id);
                    const active = harness === item.id;
                    return (
                      <button
                        key={item.id}
                        type="button"
                        disabled={!item.available || Boolean(conversation)}
                        onClick={() => setHarness(item.id)}
                        className={cn(
                          "flex items-center gap-3 rounded-sm border px-3 py-2 text-left transition-colors",
                          active ? "border-primary/60 bg-primary/10" : "border-border bg-background/20",
                          !item.available && "cursor-not-allowed opacity-40",
                          conversation && !active && "opacity-40",
                        )}
                      >
                        <span className="grid h-8 w-8 shrink-0 place-items-center rounded-sm bg-foreground/5">
                          <Icon className="h-4 w-4" />
                        </span>
                        <span className="min-w-0">
                          <span className="flex items-center gap-1.5 text-sm font-medium">
                            {item.name}
                            {active && <Check className="h-3.5 w-3.5 text-primary" />}
                          </span>
                          <span className="block truncate text-xs text-muted-foreground">
                            {item.available ? "Ready" : "Not installed"}
                          </span>
                        </span>
                      </button>
                    );
                  })}
                </div>
                <details className="mt-3 text-xs text-muted-foreground">
                  <summary className="flex cursor-pointer list-none items-center gap-1.5">
                    <Settings2 className="h-3.5 w-3.5" />
                    Session options
                  </summary>
                  <div className="mt-3 grid gap-3 sm:grid-cols-2">
                    <label className="grid gap-1">
                      <span>Working directory</span>
                      <Input value={cwd} onChange={(event) => setCwd(event.target.value)} disabled={Boolean(conversation)} />
                    </label>
                    <label className="grid gap-1">
                      <span>Model override (optional)</span>
                      <Input
                        value={model}
                        onChange={(event) => setModel(event.target.value)}
                        placeholder="Use harness default"
                        disabled={Boolean(conversation)}
                      />
                    </label>
                  </div>
                </details>
              </div>

              <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-3 py-5 sm:px-6">
                {!conversation?.messages.length && (
                  <div className="mx-auto flex max-w-lg flex-col items-center py-16 text-center">
                    <div className="mb-4 grid h-12 w-12 place-items-center rounded-full border border-primary/30 bg-primary/10">
                      <Bot className="h-5 w-5 text-primary" />
                    </div>
                    <h2 className="font-mondwest text-display text-base">A direct line to {currentHarness?.name ?? "your agent"}</h2>
                    <p className="mt-2 text-sm text-muted-foreground">
                      Attach context, select one or more skills, then describe the outcome you want.
                    </p>
                  </div>
                )}
                {conversation?.messages.map((message, index) => (
                  <div
                    key={`${message.created_at}-${index}`}
                    className={cn("flex", message.role === "user" ? "justify-end" : "justify-start")}
                  >
                    <div
                      className={cn(
                        "max-w-[92%] rounded-sm border px-4 py-3 sm:max-w-[82%]",
                        message.role === "user"
                          ? "border-primary/30 bg-primary/10"
                          : "border-border bg-background/45",
                      )}
                    >
                      <div className="mb-2 flex items-center justify-between gap-4 text-[11px] uppercase tracking-wider text-muted-foreground">
                        <span>{message.role === "user" ? "You" : currentHarness?.name}</span>
                        {message.role === "assistant" && (
                          <Button
                            ghost
                            size="icon"
                            className="h-6 w-6"
                            aria-label="Copy response"
                            onClick={() => {
                              void navigator.clipboard.writeText(message.content);
                              showToast("Response copied.", "success");
                            }}
                          >
                            <Clipboard className="h-3.5 w-3.5" />
                          </Button>
                        )}
                      </div>
                      {message.role === "assistant" ? (
                        <Markdown content={message.content} />
                      ) : (
                        <p className="whitespace-pre-wrap text-sm leading-6">{message.content}</p>
                      )}
                      {!!message.attachments?.length && (
                        <div className="mt-2 flex flex-wrap gap-1">
                          {message.attachments.map((path) => (
                            <Badge key={path}>{path.split("/").pop()}</Badge>
                          ))}
                        </div>
                      )}
                    </div>
                  </div>
                ))}
                {sending && (
                  <div className="flex items-center gap-2 text-sm text-muted-foreground">
                    <Spinner />
                    {currentHarness?.name} is working in the repository…
                  </div>
                )}
                <div ref={transcriptEndRef} />
              </div>

              <div className="border-t border-border bg-background-base/75 p-3 backdrop-blur sm:p-4">
                {!!attachments.length && (
                  <div className="mb-2 flex flex-wrap gap-2">
                    {attachments.map((attachment) => (
                      <Badge key={attachment.path} className="flex items-center gap-1.5">
                        <File className="h-3 w-3" />
                        {attachment.name}
                        <button
                          type="button"
                          aria-label={`Remove ${attachment.name}`}
                          onClick={() =>
                            setAttachments((current) =>
                              current.filter((item) => item.path !== attachment.path),
                            )
                          }
                        >
                          <X className="h-3 w-3" />
                        </button>
                      </Badge>
                    ))}
                  </div>
                )}
                {!!selectedSkills.length && (
                  <div className="mb-2 flex flex-wrap gap-1">
                    {selectedSkills.map((name) => (
                      <Badge key={name}>{name}</Badge>
                    ))}
                  </div>
                )}
                <div className="rounded-sm border border-border bg-background/45 focus-within:border-primary/50">
                  <textarea
                    value={prompt}
                    onChange={(event) => setPrompt(event.target.value)}
                    onKeyDown={onPromptKeyDown}
                    placeholder={`Message ${currentHarness?.name ?? "coding agent"}…`}
                    className="min-h-24 w-full resize-none bg-transparent px-3 pt-3 text-sm outline-none placeholder:text-muted-foreground"
                    disabled={sending}
                  />
                  <div className="flex flex-wrap items-center justify-between gap-2 px-2 pb-2">
                    <div className="flex items-center gap-1">
                      <input ref={fileInputRef} type="file" multiple className="hidden" onChange={uploadFiles} />
                      <Button
                        ghost
                        size="icon"
                        className="h-8 w-8"
                        aria-label="Attach files"
                        disabled={uploading || sending}
                        onClick={() => fileInputRef.current?.click()}
                      >
                        {uploading ? <Spinner /> : <Paperclip className="h-4 w-4" />}
                      </Button>
                      <Button
                        ghost
                        size="icon"
                        className={cn("h-8 w-8", recording && "text-red-400")}
                        aria-label={recording ? "Stop recording" : "Transcribe voice"}
                        disabled={transcribing || sending}
                        onClick={() => void toggleRecording()}
                      >
                        {transcribing ? (
                          <Spinner />
                        ) : recording ? (
                          <CircleStop className="h-4 w-4" />
                        ) : (
                          <Mic className="h-4 w-4" />
                        )}
                      </Button>
                      <SkillsMenu skills={skills} selected={selectedSkills} onChange={setSelectedSkills} compact />
                    </div>
                    <div className="flex items-center gap-2">
                      <span className="hidden text-xs text-muted-foreground sm:inline">Enter to send · Shift+Enter for newline</span>
                      <Button
                        size="sm"
                        className="uppercase"
                        disabled={!prompt.trim() || sending || !currentHarness?.available}
                        onClick={() => void submit()}
                        prefix={sending ? <Spinner /> : <Send className="h-4 w-4" />}
                      >
                        Send
                      </Button>
                    </div>
                  </div>
                </div>
              </div>
            </CardContent>
          </Card>
        </div>
      ) : (
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_minmax(20rem,0.8fr)]">
          <Card className="border-border bg-background/25">
            <CardContent className="p-5">
              <div className="mb-5">
                <h2 className="font-mondwest text-display text-base">Bind a Discord channel</h2>
                <p className="mt-1 text-sm text-muted-foreground">
                  Messages in the channel go straight to the selected coding harness. Existing unbound channels keep using Hermes normally.
                </p>
              </div>
              <div className="grid gap-4">
                <label className="grid gap-1.5 text-sm">
                  <span className="text-muted-foreground">Discord channel</span>
                  <Select value={bindingChannel} onValueChange={setBindingChannel}>
                    <SelectOption value="">Select a channel</SelectOption>
                    {channels.map((channel) => (
                      <SelectOption key={channel.id} value={channel.id}>
                        {[channel.guild, `#${channel.name}`].filter(Boolean).join(" / ")}
                      </SelectOption>
                    ))}
                  </Select>
                  {!channels.length && (
                    <span className="text-xs text-muted-foreground">
                      No Discord channels discovered yet. Start the configured gateway so Hermes can load the channel directory.
                    </span>
                  )}
                </label>
                <label className="grid gap-1.5 text-sm">
                  <span className="text-muted-foreground">Coding agent</span>
                  <Select
                    value={bindingHarness}
                    onValueChange={(value) => setBindingHarness(value as AgentHubHarness["id"])}
                  >
                    {availableHarnesses
                      .filter((item) => item.available)
                      .map((item) => (
                        <SelectOption key={item.id} value={item.id}>
                          {item.name}
                        </SelectOption>
                      ))}
                  </Select>
                </label>
                <label className="grid gap-1.5 text-sm">
                  <span className="text-muted-foreground">Working directory</span>
                  <Input value={bindingCwd} onChange={(event) => setBindingCwd(event.target.value)} />
                </label>
                <div>
                  <p className="mb-1.5 text-sm text-muted-foreground">Skills available in this channel</p>
                  <SkillsMenu skills={skills} selected={bindingSkills} onChange={setBindingSkills} />
                  {!!bindingSkills.length && (
                    <div className="mt-2 flex flex-wrap gap-1">
                      {bindingSkills.map((name) => <Badge key={name}>{name}</Badge>)}
                    </div>
                  )}
                </div>
                <Button
                  className="mt-2 uppercase sm:w-fit"
                  disabled={!bindingChannel || savingBinding}
                  onClick={() => void saveBinding()}
                  prefix={savingBinding ? <Spinner /> : <Link2 className="h-4 w-4" />}
                >
                  Bind channel
                </Button>
              </div>
            </CardContent>
          </Card>

          <Card className="border-border bg-background/25">
            <CardContent className="p-5">
              <h2 className="font-mondwest text-display text-base">Active bindings</h2>
              <div className="mt-4 space-y-2">
                {(hub?.bindings ?? []).map((binding) => {
                  const boundHarness = availableHarnesses.find((item) => item.id === binding.harness);
                  return (
                    <div key={binding.channel_id} className="flex items-start justify-between gap-3 rounded-sm border border-border bg-background/30 p-3">
                      <div className="min-w-0">
                        <p className="truncate text-sm font-medium">
                          {binding.channel_name || `Channel ${binding.channel_id}`}
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {boundHarness?.name ?? binding.harness}
                          {binding.skills.length ? ` · ${binding.skills.length} skills` : ""}
                        </p>
                      </div>
                      <Button
                        ghost
                        size="icon"
                        aria-label="Remove binding"
                        onClick={() => void removeBinding(binding)}
                      >
                        <Unlink className="h-4 w-4" />
                      </Button>
                    </div>
                  );
                })}
                {!hub?.bindings.length && (
                  <div className="py-12 text-center text-sm text-muted-foreground">
                    No channels are bound to coding agents.
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </div>
      )}
    </div>
  );
}
