/* One dashboard card: header, live data body, context menu, scoped chat. */
import { useState } from "react";
import * as ContextMenu from "@radix-ui/react-context-menu";
import {
  MessageCircle, RefreshCw, EyeOff, Trash2,
  Send, X, LoaderCircle,
} from "lucide-react";
import { DataView, useComponentData } from "./DataViews";
import { accentFor, SOURCE_ICONS } from "../lib/accents";
import { post, track, USER, type Component } from "../lib/api";

interface Props {
  c: Component;
  preview?: string; // proposal id when previewing
  onHide: (id: string) => void;
  onRemove: (id: string) => void;
}

export default function Card({ c, preview, onHide, onRemove }: Props) {
  const { data, err, refresh } = useComponentData(c, preview);
  const [chatOpen, setChatOpen] = useState(false);
  const accent = accentFor(c.type);
  const Icon = SOURCE_ICONS[c.props?.source] || accent.icon;

  return (
    <ContextMenu.Root>
      <ContextMenu.Trigger asChild>
        <div
          className="relative h-full flex flex-col bg-surface border border-line rounded-xl overflow-hidden hover:border-line-2 hover:shadow-[0_4px_24px_rgba(0,0,0,.35)] transition-all group"
          onClickCapture={() => track("click", c.id)}
          onMouseEnter={() => ((c as any)._t0 = performance.now())}
          onMouseLeave={() => {
            const t0 = (c as any)._t0;
            if (t0) {
              const s = (performance.now() - t0) / 1000;
              if (s > 0.8) track("focus_dwell", c.id, { seconds: Math.round(s * 10) / 10 });
            }
          }}
        >
          <div className={`absolute top-0 left-0 right-0 h-px bg-gradient-to-r ${accent.line} to-transparent`} />
          <div className="drag-handle flex items-center justify-between h-10 px-3 border-b border-line shrink-0 cursor-grab active:cursor-grabbing select-none">
            <div className="flex items-center gap-2 min-w-0">
              <span className={`w-6 h-6 rounded-md ${accent.bg} ${accent.fg} flex items-center justify-center shrink-0`}>
                <Icon size={13} />
              </span>
              <span className="text-[13px] font-semibold truncate">{c.title}</span>
            </div>
            {!preview && (
              <div className="flex gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
                   onMouseDown={(e) => e.stopPropagation()}>
                <IconBtn title="Ask this component" onClick={() => { setChatOpen(true); track("component_chat_open", c.id); }}>
                  <MessageCircle size={13} />
                </IconBtn>
                <IconBtn title="Refresh" onClick={refresh}><RefreshCw size={13} /></IconBtn>
                <IconBtn title="Hide" onClick={() => onHide(c.id)}><EyeOff size={13} /></IconBtn>
              </div>
            )}
          </div>
          <div className="flex-1 overflow-auto px-3.5 py-3 min-h-0">
            <DataView c={c} data={data} err={err} />
          </div>
          {chatOpen && <ComponentChat c={c} onClose={() => setChatOpen(false)} onChanged={refresh} />}
        </div>
      </ContextMenu.Trigger>
      {!preview && (
        <ContextMenu.Portal>
          <ContextMenu.Content className="min-w-[210px] bg-surface-2 border border-line-2 rounded-xl p-1.5 shadow-2xl shadow-black/60 z-[100] text-[13px]">
            <CtxLabel>{c.title}</CtxLabel>
            <CtxItem onSelect={() => { setChatOpen(true); track("component_chat_open", c.id); }}>
              <MessageCircle size={13} /> Ask this component…
            </CtxItem>
            <CtxItem onSelect={refresh}><RefreshCw size={13} /> Refresh data</CtxItem>
            <ContextMenu.Separator className="h-px bg-line my-1 mx-1.5" />
            <CtxItem onSelect={() => onHide(c.id)}><EyeOff size={13} /> Hide</CtxItem>
            <CtxItem destructive onSelect={() => onRemove(c.id)}><Trash2 size={13} /> Remove</CtxItem>
          </ContextMenu.Content>
        </ContextMenu.Portal>
      )}
    </ContextMenu.Root>
  );
}

function IconBtn({ children, onClick, title }: any) {
  return (
    <button
      title={title}
      onClick={(e) => { e.stopPropagation(); onClick(); }}
      className="w-6.5 h-6.5 flex items-center justify-center rounded-md text-ink-3 hover:text-ink hover:bg-surface-2"
    >
      {children}
    </button>
  );
}

function CtxItem({ children, onSelect, destructive }: any) {
  return (
    <ContextMenu.Item
      onSelect={onSelect}
      className={`flex items-center gap-2.5 px-3 py-2 rounded-lg cursor-pointer outline-none data-[highlighted]:bg-surface ${destructive ? "text-red" : "text-ink"}`}
    >
      {children}
    </ContextMenu.Item>
  );
}

function CtxLabel({ children }: any) {
  return <div className="px-3 pt-1.5 pb-1 text-[10.5px] font-semibold uppercase tracking-wider text-ink-3 truncate">{children}</div>;
}

/* Scoped chat: renders inside the card as an overlay panel */
function ComponentChat({ c, onClose, onChanged }: { c: Component; onClose: () => void; onChanged: () => void }) {
  const [msgs, setMsgs] = useState<{ who: string; text: string }[]>([
    { who: "hermes", text: "Scoped to this component — ask about its data or tell me to change it." },
  ]);
  const [busy, setBusy] = useState(false);
  const [input, setInput] = useState("");

  const send = async () => {
    const text = input.trim();
    if (!text || busy) return;
    setInput("");
    setMsgs((m) => [...m, { who: "you", text }]);
    setBusy(true);
    try {
      const r = await post(`/api/component/${c.id}/chat`, { user_id: USER, text });
      setMsgs((m) => [...m, { who: "hermes", text: r.reply }]);
      onChanged();
    } catch (e: any) {
      setMsgs((m) => [...m, { who: "hermes", text: `⚠ ${e.message}` }]);
    }
    setBusy(false);
  };

  return (
    <div className="absolute inset-0 z-20 bg-surface/97 backdrop-blur-sm flex flex-col rounded-xl border border-teal/40"
         onMouseDown={(e) => e.stopPropagation()}>
      <div className="flex items-center justify-between px-3.5 h-10 border-b border-line shrink-0">
        <span className="text-[13px] font-semibold text-teal truncate">◎ {c.title}</span>
        <button onClick={onClose} className="text-ink-3 hover:text-ink"><X size={15} /></button>
      </div>
      <div className="flex-1 overflow-auto px-3.5 py-2.5 space-y-2.5 min-h-0">
        {msgs.map((m, i) => (
          <div key={i} className="text-[13px] leading-relaxed">
            <span className={`block text-[10px] font-bold uppercase tracking-wider mb-0.5 ${m.who === "you" ? "text-teal" : "text-gold"}`}>{m.who}</span>
            <span className="text-ink-2 whitespace-pre-wrap">{m.text}</span>
          </div>
        ))}
        {busy && <LoaderCircle size={15} className="spin text-ink-3" />}
      </div>
      <div className="flex items-center border-t border-line shrink-0">
        <input
          autoFocus
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send()}
          placeholder="Ask or change this component…"
          className="flex-1 bg-transparent px-3.5 py-2.5 text-[13px] outline-none placeholder:text-ink-3"
        />
        <button onClick={send} className="pr-3 text-ink-3 hover:text-gold"><Send size={15} /></button>
      </div>
    </div>
  );
}
