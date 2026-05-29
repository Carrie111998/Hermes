import { fetchJSON } from "@/lib/api";

export interface ChatUploadResponse {
  path: string;
  name: string;
  mime_type: string;
  size: number;
}

export function uploadChatAttachment(file: File): Promise<ChatUploadResponse> {
  return fetchJSON<ChatUploadResponse>("/api/chat/uploads", {
    method: "POST",
    headers: {
      "Content-Type": file.type || "application/octet-stream",
      "X-Hermes-Filename": file.name || "upload.bin",
    },
    body: file,
  });
}
