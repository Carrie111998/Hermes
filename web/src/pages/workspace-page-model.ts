import type { WorkspaceBinding, WorkspaceProject } from "@/lib/api";

export function primaryOnlineBinding(
  project: WorkspaceProject,
): WorkspaceBinding | null {
  const online = project.bindings.filter(
    (binding) => binding.status === "online" && binding.chat_available !== false,
  );
  return online.find((binding) => binding.is_primary) ?? online[0] ?? null;
}

export function workspaceChatHref(binding: WorkspaceBinding): string {
  return `/chat?binding=${encodeURIComponent(binding.binding_id)}`;
}

export function workspaceResumeHref(sessionId: string): string {
  return `/chat?resume=${encodeURIComponent(sessionId)}`;
}
