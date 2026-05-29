import { Button } from "@nous-research/ui/ui/components/button";
import { FolderOpen, ImagePlus, Loader2, Paperclip, Send, X } from "lucide-react";
import { useRef, useState } from "react";

import type { WebChatAttachment } from "@/components/chat/contracts";

interface ChatComposerProps {
  attachments: WebChatAttachment[];
  attachmentPath: string;
  attachError: string | null;
  uploadingAttachment: boolean;
  canSubmit: boolean;
  input: string;
  sessionId: string | null;
  setAttachmentPath: (value: string) => void;
  setInput: (value: string) => void;
  attachLocalPath: () => void;
  uploadAttachment: (file: File) => void;
  removeAttachment: (id: string) => void;
  sendCurrentInput: () => void;
}

export function ChatComposer({
  attachments,
  attachmentPath,
  attachError,
  uploadingAttachment,
  canSubmit,
  input,
  sessionId,
  setAttachmentPath,
  setInput,
  attachLocalPath,
  uploadAttachment,
  removeAttachment,
  sendCurrentInput,
}: ChatComposerProps) {
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const [pathOpen, setPathOpen] = useState(false);

  return (
    <div className="relative z-1 shrink-0 border-t border-current/10 bg-[#151615]/88 px-4 py-3 backdrop-blur sm:px-6">
      <div className="mx-auto max-w-4xl">
        {attachments.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {attachments.map((a) => (
              <span
                key={a.id}
                className="inline-flex max-w-full items-center gap-2 rounded-md border border-[#8ef7e0]/25 bg-[#8ef7e0]/10 px-2.5 py-1.5 text-xs"
              >
                <ImagePlus className="h-3.5 w-3.5 shrink-0 text-[#9df8e8]" />
                <span className="truncate">{a.name}</span>
                {a.meta && <span className="text-[#e7e0d1]/45">{a.meta}</span>}
                <button
                  type="button"
                  onClick={() => removeAttachment(a.id)}
                  className="text-[#e7e0d1]/50 hover:text-[#f5efe1]"
                  aria-label={`Remove ${a.name}`}
                >
                  <X className="h-3.5 w-3.5" />
                </button>
              </span>
            ))}
          </div>
        )}

        <div className="flex items-end gap-2 rounded-xl border border-white/12 bg-[#202120] p-2 shadow-[0_14px_50px_rgba(0,0,0,0.28)]">
          <input
            ref={fileInputRef}
            type="file"
            accept="image/*,video/*,audio/*"
            className="hidden"
            onChange={(ev) => {
              const file = ev.currentTarget.files?.[0];
              ev.currentTarget.value = "";
              if (file) uploadAttachment(file);
            }}
          />
          <Button
            ghost
            size="icon"
            disabled={!sessionId || uploadingAttachment}
            onClick={() => fileInputRef.current?.click()}
            aria-label="Upload media"
            title="Upload media"
            className="mb-1 shrink-0 rounded-full bg-white/5 text-[#e7e0d1]/70 hover:text-[#9df8e8]"
          >
            {uploadingAttachment ? (
              <Loader2 className="h-4 w-4 animate-spin" />
            ) : (
              <Paperclip className="h-4 w-4" />
            )}
          </Button>
          <Button
            ghost
            size="icon"
            onClick={() => {
              setPathOpen((open) => {
                if (open) setAttachmentPath("");
                return !open;
              });
            }}
            aria-label="Attach by path"
            title="Attach by path"
            className="mb-1 hidden shrink-0 rounded-full bg-white/5 text-[#e7e0d1]/70 hover:text-[#9df8e8] sm:inline-flex"
          >
            <FolderOpen className="h-4 w-4" />
          </Button>
          <textarea
            value={input}
            onChange={(ev) => setInput(ev.target.value)}
            onKeyDown={(ev) => {
              if (ev.key === "Enter" && !ev.shiftKey) {
                ev.preventDefault();
                sendCurrentInput();
              }
            }}
            placeholder="Message Hermes..."
            className="max-h-40 min-h-12 flex-1 resize-none bg-transparent px-1 py-3 text-sm text-[#f5efe1] outline-none placeholder:text-[#e7e0d1]/35"
          />
          <Button
            size="icon"
            disabled={!canSubmit || !input.trim() || uploadingAttachment}
            onClick={sendCurrentInput}
            aria-label="Send"
            className="mb-1 shrink-0 rounded-full bg-[#9df8e8] text-[#101111] hover:bg-[#c6fff4] disabled:opacity-40"
          >
            <Send className="h-4 w-4" />
          </Button>
        </div>

        {pathOpen && (
          <div className="mt-2 flex gap-2">
            <input
              value={attachmentPath}
              onChange={(ev) => setAttachmentPath(ev.target.value)}
              onKeyDown={(ev) => {
                if (ev.key === "Enter") {
                  ev.preventDefault();
                  attachLocalPath();
                }
              }}
              placeholder="/absolute/path/to/image-or-media"
              className="min-w-0 flex-1 rounded-md border border-white/12 bg-black/20 px-3 py-2 text-xs text-[#f5efe1] outline-none placeholder:text-[#e7e0d1]/35 focus-visible:border-[#9df8e8]/45"
            />
            <Button
              size="sm"
              onClick={attachLocalPath}
              disabled={!sessionId || !attachmentPath.trim()}
              className="rounded-md"
            >
              attach
            </Button>
          </div>
        )}
        {attachError && <div className="mt-2 text-xs text-red-200">{attachError}</div>}
      </div>
    </div>
  );
}
