import { useCallback, useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router";
import {
  ChevronRight,
  BrainCircuit,
  CircleAlert,
  Clock3,
  FolderGit2,
  MessageSquare,
  MessageSquarePlus,
  RefreshCw,
  ShieldCheck,
  Undo2,
  Wifi,
  WifiOff,
  X,
} from "lucide-react";

import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@nous-research/ui/ui/components/card";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { Input } from "@nous-research/ui/ui/components/input";
import { Typography } from "@nous-research/ui/ui/components/typography/index";

import { useProfileScope } from "@/contexts/useProfileScope";
import {
  api,
  type WorkspaceApproval,
  type WorkspaceLearningCandidate,
  type WorkspaceProject,
} from "@/lib/api";
import { cn, timeAgo } from "@/lib/utils";
import {
  primaryOnlineBinding,
  workspaceChatHref,
  workspaceResumeHref,
} from "./workspace-page-model";

export default function WorkspacePage() {
  const navigate = useNavigate();
  const { profile } = useProfileScope();
  const [projects, setProjects] = useState<WorkspaceProject[]>([]);
  const [approvals, setApprovals] = useState<WorkspaceApproval[]>([]);
  const [learningCandidates, setLearningCandidates] = useState<WorkspaceLearningCandidate[]>([]);
  const [approvalBusy, setApprovalBusy] = useState<string | null>(null);
  const [learningBusy, setLearningBusy] = useState<string | null>(null);
  const [contextBusy, setContextBusy] = useState(false);
  const [contextEditing, setContextEditing] = useState(false);
  const [notionDraft, setNotionDraft] = useState("");
  const [slackDraft, setSlackDraft] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [response, approvalResponse, learningResponse] = await Promise.all([
        api.getWorkspaceProjects(),
        api.getWorkspaceApprovals(),
        api.getWorkspaceLearningCandidates(),
      ]);
      setProjects(response.projects);
      setApprovals(approvalResponse.approvals);
      setLearningCandidates(learningResponse.candidates);
      setSelectedId((current) =>
        response.projects.some((project) => project.id === current)
          ? current
          : (response.projects[0]?.id ?? null),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Workspace unavailable");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      void load();
    }, 0);

    return () => window.clearTimeout(timer);
  }, [load, profile]);

  const selected = useMemo(
    () => projects.find((project) => project.id === selectedId) ?? projects[0] ?? null,
    [projects, selectedId],
  );

  const startChat = useCallback(
    (project: WorkspaceProject) => {
      const binding = primaryOnlineBinding(project);
      if (binding) navigate(workspaceChatHref(binding));
    },
    [navigate],
  );

  const beginContextEdit = useCallback((project: WorkspaceProject) => {
    setSlackDraft((project.context?.slack_channel_ids || []).join(", "));
    setNotionDraft((project.context?.notion_page_ids || []).join(", "));
    setContextEditing(true);
  }, []);

  const saveContext = useCallback(async (project: WorkspaceProject) => {
    const split = (value: string) => value
      .split(/[\s,]+/)
      .map((item) => item.trim())
      .filter(Boolean);
    setContextBusy(true);
    setError(null);
    try {
      const response = await api.updateWorkspaceProjectContext(project.id, {
        notion_page_ids: split(notionDraft),
        slack_channel_ids: split(slackDraft),
      });
      setProjects((current) => current.map((item) => (
        item.id === project.id ? { ...item, context: response.context } : item
      )));
      setContextEditing(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Context update failed");
    } finally {
      setContextBusy(false);
    }
  }, [notionDraft, slackDraft]);

  const decideApproval = useCallback(async (approval: WorkspaceApproval, approved: boolean) => {
    const requestId = approval.request.requestId;
    setApprovalBusy(requestId);
    setError(null);
    try {
      await api.decideWorkspaceApproval(requestId, approved);
      setApprovals((current) =>
        current.filter((candidate) => candidate.request.requestId !== requestId),
      );
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "Approval failed");
    } finally {
      setApprovalBusy(null);
    }
  }, []);

  const decideLearningCandidate = useCallback(
    async (candidate: WorkspaceLearningCandidate, action: "approve" | "reject" | "rollback") => {
      const candidateId = candidate.candidate_id;
      setLearningBusy(candidateId);
      setError(null);
      try {
        if (action === "approve") {
          const response = await api.approveWorkspaceLearningCandidate(candidateId);
          setLearningCandidates((current) =>
            current.map((item) =>
              item.candidate_id === candidateId ? response.candidate : item,
            ),
          );
        } else if (action === "reject") {
          await api.rejectWorkspaceLearningCandidate(
            candidateId,
            "Rejected from Project Workspace review",
          );
          setLearningCandidates((current) =>
            current.filter((item) => item.candidate_id !== candidateId),
          );
        } else {
          await api.rollbackWorkspaceLearningCandidate(
            candidateId,
            "Rollback requested from Project Workspace",
          );
          setLearningCandidates((current) =>
            current.filter((item) => item.candidate_id !== candidateId),
          );
        }
      } catch (reason) {
        setError(reason instanceof Error ? reason.message : "Learning action failed");
      } finally {
        setLearningBusy(null);
      }
    },
    [],
  );

  if (loading && projects.length === 0) {
    return (
      <div className="flex min-h-[18rem] items-center justify-center gap-2 text-sm text-muted-foreground">
        <Spinner />
        Loading project workspace…
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-[1440px] flex-1 flex-col gap-4 p-3 sm:p-5 lg:p-6">
      <header className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <Typography className="block text-xl font-semibold">Project Workspace</Typography>
          <Typography className="block max-w-2xl text-sm text-muted-foreground">
            Continue project conversations, check device availability, and start a scoped Hermes chat without exposing device paths.
          </Typography>
        </div>
        <Button
          aria-label="Refresh workspace"
          className="min-h-12 px-4 sm:min-h-9"
          onClick={() => void load()}
          size="sm"
        >
          <RefreshCw className={cn("size-4", loading && "animate-spin")} />
          Refresh
        </Button>
      </header>

      {error && (
        <div className="flex items-start gap-2 rounded-lg border border-destructive/30 bg-destructive/5 p-3 text-sm text-destructive" role="alert">
          <CircleAlert className="mt-0.5 size-4 shrink-0" />
          <span>{error}</span>
        </div>
      )}

      {approvals.length > 0 && (
        <Card className="border-amber-500/30">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <ShieldCheck className="size-4" />
              Push approvals
              <Badge tone="secondary">{approvals.length}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 xl:grid-cols-2">
            {approvals.map((approval) => {
              const request = approval.request;
              const busy = approvalBusy === request.requestId;
              return (
                <article className="rounded-xl border p-3" key={request.requestId}>
                  <p className="truncate text-sm font-medium">{request.remoteUrl}</p>
                  <p className="mt-1 text-xs text-muted-foreground">
                    {request.destinationBranch} · {request.commitSha.slice(0, 12)} · digest {request.changeSetDigest.slice(0, 12)}
                  </p>
                  <div className="mt-3 grid grid-cols-2 gap-2">
                    <Button
                      aria-label={`Deny push ${request.commitSha.slice(0, 8)}`}
                      className="min-h-11"
                      disabled={busy}
                      onClick={() => void decideApproval(approval, false)}
                    >
                      <X className="size-4" />
                      Deny
                    </Button>
                    <Button
                      aria-label={`Approve push ${request.commitSha.slice(0, 8)}`}
                      className="min-h-11"
                      disabled={busy}
                      onClick={() => void decideApproval(approval, true)}
                    >
                      {busy ? <Spinner /> : <ShieldCheck className="size-4" />}
                      Approve
                    </Button>
                  </div>
                </article>
              );
            })}
          </CardContent>
        </Card>
      )}

      {learningCandidates.length > 0 && (
        <Card className="border-violet-500/30">
          <CardHeader>
            <CardTitle className="flex items-center gap-2">
              <BrainCircuit className="size-4" />
              Learning proposals
              <Badge tone="secondary">{learningCandidates.length}</Badge>
            </CardTitle>
          </CardHeader>
          <CardContent className="grid gap-3 xl:grid-cols-2">
            {learningCandidates.map((candidate) => {
              const busy = learningBusy === candidate.candidate_id;
              const content = candidate.proposal.content;
              const proposalText =
                typeof content === "string" ? content : JSON.stringify(candidate.proposal);
              return (
                <article className="rounded-xl border p-3" key={candidate.candidate_id}>
                  <div className="flex flex-wrap items-center gap-2">
                    <Badge tone="outline">{candidate.destination}</Badge>
                    <Badge tone="outline">{candidate.risk} risk</Badge>
                    <Badge tone="secondary">{candidate.status.replaceAll("_", " ")}</Badge>
                  </div>
                  <p className="mt-3 break-words text-sm font-medium">{proposalText}</p>
                  <p className="mt-2 text-xs text-muted-foreground">
                    digest {candidate.content_digest.slice(0, 12)} · updated {timeAgo(candidate.updated_at)}
                  </p>
                  <div className="mt-2 flex flex-wrap gap-1 text-xs text-muted-foreground">
                    {candidate.provenance.slice(0, 3).map((source) => (
                      <span className="rounded bg-muted px-1.5 py-0.5" key={`${source.source}:${source.ref}`}>
                        {source.source} · {source.ref}
                      </span>
                    ))}
                  </div>
                  {candidate.status === "approval_pending" && (
                    <div className="mt-3 grid grid-cols-2 gap-2">
                      <Button
                        aria-label={`Reject learning proposal ${candidate.candidate_id}`}
                        className="min-h-11"
                        disabled={busy}
                        onClick={() => void decideLearningCandidate(candidate, "reject")}
                      >
                        <X className="size-4" />
                        Reject
                      </Button>
                      <Button
                        aria-label={`Approve learning proposal ${candidate.candidate_id}`}
                        className="min-h-11"
                        disabled={busy}
                        onClick={() => void decideLearningCandidate(candidate, "approve")}
                      >
                        {busy ? <Spinner /> : <ShieldCheck className="size-4" />}
                        Approve
                      </Button>
                    </div>
                  )}
                  {["applied", "apply_uncertain", "rollback_uncertain"].includes(candidate.status) && (
                    <Button
                      aria-label={`Rollback learning proposal ${candidate.candidate_id}`}
                      className="mt-3 min-h-11 w-full"
                      disabled={busy}
                      onClick={() => void decideLearningCandidate(candidate, "rollback")}
                    >
                      {busy ? <Spinner /> : <Undo2 className="size-4" />}
                      Roll back and quarantine
                    </Button>
                  )}
                </article>
              );
            })}
          </CardContent>
        </Card>
      )}

      {projects.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="flex min-h-[18rem] flex-col items-center justify-center text-center">
            <FolderGit2 className="mb-3 size-9 text-muted-foreground" />
            <CardTitle>No registered projects</CardTitle>
            <p className="mt-2 max-w-md text-sm text-muted-foreground">
              Register a local repository from Hermes Desktop. It will appear here as an opaque device binding.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="grid min-h-0 flex-1 gap-4 lg:grid-cols-[minmax(240px,0.34fr)_minmax(0,1fr)]">
          <nav aria-label="Workspace projects" className="grid content-start gap-2 sm:grid-cols-2 lg:grid-cols-1">
            {projects.map((project) => {
              const online = project.bindings.filter((binding) => binding.status === "online").length;
              const active = project.id === selected?.id;
              return (
                <button
                  className={cn(
                    "rounded-xl border p-3 text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                    active && "border-primary/40 bg-accent",
                  )}
                  key={project.id}
                  onClick={() => {
                    setSelectedId(project.id);
                    setContextEditing(false);
                  }}
                  type="button"
                >
                  <span className="flex items-center justify-between gap-2">
                    <span className="truncate font-medium">{project.icon || "◆"} {project.name}</span>
                    <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                  </span>
                  <span className="mt-2 flex items-center gap-1.5 text-xs text-muted-foreground">
                    {online > 0 ? <Wifi className="size-3.5 text-emerald-500" /> : <WifiOff className="size-3.5" />}
                    {online}/{project.bindings.length} devices online
                  </span>
                </button>
              );
            })}
          </nav>

          {selected && (
            <section className="min-w-0 space-y-4" aria-label={`${selected.name} workspace`}>
              <Card>
                <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
                  <div>
                    <CardTitle>{selected.name}</CardTitle>
                    <p className="mt-1 text-sm text-muted-foreground">
                      {selected.description || "Project-scoped conversations and device runners."}
                    </p>
                  </div>
                  <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row">
                    <Button
                      className="min-h-12 sm:min-h-9"
                      onClick={() => beginContextEdit(selected)}
                    >
                      Edit context
                    </Button>
                    <Button
                      className="min-h-12 sm:min-h-9"
                      disabled={!primaryOnlineBinding(selected)}
                      onClick={() => startChat(selected)}
                    >
                      <MessageSquarePlus className="size-4" />
                      New scoped chat
                    </Button>
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="flex flex-wrap gap-2">
                    {selected.bindings.map((binding) => (
                      <span className="inline-flex flex-wrap gap-1" key={`${binding.runner_id}:${binding.binding_id}`}>
                        <Badge tone={binding.status === "online" ? "secondary" : "outline"}>
                          {binding.status === "online" ? <Wifi className="mr-1 size-3" /> : <WifiOff className="mr-1 size-3" />}
                          {binding.label} · {binding.status}
                        </Badge>
                        {binding.capabilities?.includes("worker.codex") && (
                          <Badge tone="outline">Audited Codex worker</Badge>
                        )}
                        {binding.chat_available === false && (
                          <Badge tone="outline">Remote commands</Badge>
                        )}
                      </span>
                    ))}
                  </div>
                  <div className="mt-3 flex flex-wrap gap-2 text-xs text-muted-foreground">
                    <Badge tone="outline">
                      Slack allowlist · {selected.context?.slack_channel_ids.length || 0}
                    </Badge>
                    <Badge tone="outline">
                      Notion pages · {selected.context?.notion_page_ids.length || 0}
                    </Badge>
                  </div>
                  {contextEditing && (
                    <div className="mt-4 space-y-3 rounded-lg border bg-muted/20 p-3">
                      <label className="block space-y-1.5 text-sm">
                        <span className="font-medium">Slack channel IDs</span>
                        <Input
                          onChange={(event) => setSlackDraft(event.target.value)}
                          placeholder="C012ABC, C034DEF"
                          value={slackDraft}
                        />
                      </label>
                      <label className="block space-y-1.5 text-sm">
                        <span className="font-medium">Notion page IDs</span>
                        <Input
                          onChange={(event) => setNotionDraft(event.target.value)}
                          placeholder="32-character page ID"
                          value={notionDraft}
                        />
                      </label>
                      <p className="text-xs text-muted-foreground">
                        Slack search is restricted to this saved allowlist. Separate IDs with commas or spaces.
                      </p>
                      <div className="flex flex-col gap-2 sm:flex-row">
                        <Button
                          className="min-h-12 sm:min-h-9"
                          disabled={contextBusy}
                          onClick={() => void saveContext(selected)}
                        >
                          {contextBusy && <Spinner className="size-4" />}
                          Save context
                        </Button>
                        <Button
                          className="min-h-12 sm:min-h-9"
                          disabled={contextBusy}
                          onClick={() => setContextEditing(false)}
                        >
                          Cancel
                        </Button>
                      </div>
                    </div>
                  )}
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle className="flex items-center gap-2">
                    <MessageSquare className="size-4" />
                    Conversations
                  </CardTitle>
                </CardHeader>
                <CardContent className="space-y-2">
                  {selected.conversations.length === 0 ? (
                    <p className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground">
                      No conversations in this project yet.
                    </p>
                  ) : (
                    selected.conversations.map((conversation) => (
                      <button
                        className="flex w-full items-center gap-3 rounded-lg border p-3 text-left transition-colors hover:bg-accent/50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        key={conversation.id}
                        onClick={() => navigate(workspaceResumeHref(conversation.id))}
                        type="button"
                      >
                        <span className={cn("size-2 shrink-0 rounded-full", conversation.is_active ? "bg-emerald-500" : "bg-muted-foreground/35")} />
                        <span className="min-w-0 flex-1">
                          <span className="block truncate text-sm font-medium">
                            {conversation.title || conversation.preview || "Untitled conversation"}
                          </span>
                          <span className="mt-1 flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
                            <span>{conversation.source || "Hermes"}</span>
                            <span>{conversation.message_count} messages</span>
                            {conversation.last_active && (
                              <span className="inline-flex items-center gap-1">
                                <Clock3 className="size-3" />
                                {timeAgo(conversation.last_active)}
                              </span>
                            )}
                          </span>
                        </span>
                        <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                      </button>
                    ))
                  )}
                </CardContent>
              </Card>
            </section>
          )}
        </div>
      )}
    </div>
  );
}
