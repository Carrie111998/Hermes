import type { ConnectionState } from "@/lib/gatewayClient";
import type { ToolEntry } from "@/components/ToolCall";

export interface WebChatSessionInfo {
  cwd?: string;
  model?: string;
  provider?: string;
  profile_name?: string;
  credential_warning?: string;
}

export interface WebChatSessionCreateResult {
  session_id: string;
  info?: WebChatSessionInfo;
}

export interface WebChatSessionResumeResult extends WebChatSessionCreateResult {
  resumed?: string;
  messages?: Array<{ role?: string; text?: string; name?: string; context?: string }>;
}

export type WebChatRole = "user" | "assistant" | "system" | "tool";
export type WebChatStatus = "streaming" | "complete" | "error";

export interface WebChatMessage {
  id: string;
  role: WebChatRole;
  text: string;
  status?: WebChatStatus;
  attachments?: WebChatAttachment[];
}

export interface WebChatAttachment {
  id: string;
  kind: "image" | "file";
  name: string;
  path: string;
  text: string;
  meta?: string;
}

export interface WebChatImageAttachResult {
  attached?: boolean;
  path?: string;
  name?: string;
  text?: string;
  count?: number;
  width?: number;
  height?: number;
  token_estimate?: number;
  message?: string;
}

export interface WebChatDetectDropResult extends WebChatImageAttachResult {
  matched?: boolean;
  is_image?: boolean;
}

export type WebChatPendingPrompt =
  | { kind: "clarify"; requestId: string; question: string; choices?: string[] }
  | { kind: "approval"; command: string; description: string }
  | { kind: "sudo"; requestId: string; password: string }
  | {
      kind: "secret";
      requestId: string;
      envVar: string;
      prompt: string;
      value: string;
    };

export interface WebChatState {
  state: ConnectionState;
  sessionId: string | null;
  info: WebChatSessionInfo;
  messages: WebChatMessage[];
  tools: ToolEntry[];
  currentStatus: string;
  running: boolean;
  error: string | null;
  pendingPrompt: WebChatPendingPrompt | null;
  attachments: WebChatAttachment[];
  attachmentPath: string;
  attachError: string | null;
  input: string;
}
