/* One dashboard card: header, live data body, context menu, scoped chat,
   and pop-out (maximize into a Dialog with a full-size view + chat tab). */
import { useEffect, useRef, useState } from "react";
import * as ContextMenu from "@radix-ui/react-context-menu";
import {
  MessageCircle, RefreshCw, EyeOff, Trash2, Send, LoaderCircle, Maximize2,
} from "lucide-react";
import { DataView, useComponentData } from "./DataViews";
import { iconFor } from "../lib/accents";
import { post, track, USER, type Component } from "../lib/api";
import {
  Button, Dialog, DialogContent, DialogHeader, Tip, Tabs, TabsList,
  TabsTrigger, TabsContent,
} from "./ui";
import Prose from "./Prose";

interface Props {
  c: Component;
  preview?: string; // proposal id when previewing
  onHide: (id: string) => void;
  onRemove: (id: string) => void;
}

export default function Card({ c, preview, onHide, onRemove }: Props) {
  const { data, err, refresh, flash } = useComponentData(c, preview);
  const [chatOpen, setChatOpen] = useState(false);
  const [maxOpen, setMaxOpen] = useState(false);
  const Icon = iconFor(c.type, c.props?.source);

  return (
    <>
    <ContextMenu.Root>
      <ContextMenu.Trigger asChild>
        <div
          className="card-surface relative h-full flex flex-col rounded-[10px] overflow-hidden group"
          onClickCapture={() => track("click", c.id)}
          onDoubleClick={(e) => {
            if ((e.target as HTMLElement).closest(".drag-handle")) {
              setMaxOpen(true);
              track("maximize", c.id);
            }
          }}
          onMouseEnter={() => ((c as any)._t0 = performance.now())}
          onMouseLeave={() => {
            const t0 = (c as any)._t0;
            if (t0) {
              const s = (performance.now() - t0) / 1000;
              if (s > 0.8) track("focus_dwell", c.id, { seconds: Math.round(s * 10) / 10 });
            }
          }}
        >
          <div className="drag-handle flex items-center justify-between h-9 pl-3.5 pr-1.5 border-b border-line shrink-0 cursor-grab active:cursor-grabbing select-none">
            <div className="flex items-center gap-2 min-w-0">
              <Icon size={14} className="text-ink-4 shrink-0" strokeWidth={1.75} />
              <span className="text-[13px] w510 text-ink-2 truncate">{c.title}</span>
              <span className={`w-1.5 h-1.5 rounded-full shrink-0 transition-all duration-500 ${flash ? "bg-green shadow-[0_0_8px] shadow-green scale-110" : "bg-transparent"}`} />
            </div>
            {!preview && (
              <div className="flex gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity"
                   onMouseDown={(e) => e.stopPropagation()}>
                <Tip label="Ask this component">
                  <Button variant="ghost" size="icon"
                          onClick={(e) => { e.stopPropagation(); setChatOpen(true); track("component_chat_open", c.id); }}>
                    <MessageCircle size={13} />
                  </Button>
                </Tip>
                <Tip label="Pop out">
                  <Button variant="ghost" size="icon"
                          onClick={(e) => { e.stopPropagation(); setMaxOpen(true); track("maximize", c.id); }}>
                    <Maximize2 size={13} />
                  </Button>
                </Tip>
                <Tip label="Refresh">
                  <Button variant="ghost" size="icon" onClick={(e) => { e.stopPropagation(); refresh(); }}>
                    <RefreshCw size={13} />
                  </Button>
                </Tip>
                <Tip label="Hide">
                  <Button variant="ghost" size="icon" onClick={(e) => { e.stopPropagation(); onHide(c.id); }}>
                    <EyeOff size={13} />
                  </Button>
                </Tip>
              </div>
            )}
          </div>
          <CardBody c={c} data={data} err={err} onPopOut={() => { setMaxOpen(true); track("maximize", c.id); }} />
          {chatOpen && <ComponentChat c={c} onClose={() => setChatOpen(false)} onChanged={refresh} />}
        </div>
      </ContextMenu.Trigger>
      {!preview && (
        <ContextMenu.Portal>
          <ContextMenu.Content className="min-w-[210px] bg-surface-2 border border-line-2 rounded-[10px] p-1 shadow-[0_8px_30px_rgba(0,0,0,.5),0_0_0_1px_rgba(255,255,255,.04)] z-[100] text-[13px]">
            <CtxLabel>{c.title}</CtxLabel>
            <CtxItem onSelect={() => setMaxOpen(true)}><Maximize2 size={13} /> Pop out</CtxItem>
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

    {/* ===== pop-out dialog: full-size data + side-by-side scoped chat ===== */}
    <Dialog open={maxOpen} onOpenChange={setMaxOpen}>
      <DialogContent>
        <DialogHeader
          title={<span className="inline-flex items-center gap-2"><Icon size={15} className="text-ink-4" /> {c.title}</span>}
          subtitle={c.props?.source || c.type}>
          <Tip label="Refresh"><Button variant="ghost" size="icon" onClick={refresh}><RefreshCw size={14} /></Button></Tip>
        </DialogHeader>
        <Tabs defaultValue="view" className="flex-1 min-h-0 flex flex-col">
          <div className="px-4 pt-3 shrink-0">
            <TabsList>
              <TabsTrigger value="view">View</TabsTrigger>
              <TabsTrigger value="chat"><MessageCircle size={12} /> Ask Hermes</TabsTrigger>
            </TabsList>
          </div>
          <TabsContent value="view" className="flex-1 min-h-0 overflow-auto px-5 py-4 data-[state=inactive]:hidden">
            <div className="h-full min-h-[280px] flex flex-col [&>*]:flex-1 [&>*]:min-h-0">
              <DataView c={c} data={data} err={err} />
            </div>
          </TabsContent>
          <TabsContent value="chat" className="flex-1 min-h-0 flex data-[state=inactive]:hidden">
            <MaximizedChat c={c} onChanged={refresh} />
          </TabsContent>
        </Tabs>
      </DialogContent>
    </Dialog>
    </>
  );
}

function CardBody({ c, data, err, onPopOut }: { c: Component; data: any; err: string; onPopOut: () => void }) {
  const ref = useRef<HTMLDivElement>(null);
  const [overflow, setOverflow] = useState(0);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const check = () => setOverflow(Math.max(0, el.scrollHeight - el.clientHeight));
    check();
    const ro = new ResizeObserver(check);
    ro.observe(el);
    const mo = new MutationObserver(check);
    mo.observe(el, { childList: true, subtree: true });
    return () => { ro.disconnect(); mo.disconnect(); };
  }, [c.id]);
  return (
    <div className="relative flex-1 min-h-0 flex flex-col">
      <div ref={ref} className="body-fade flex-1 overflow-auto px-3.5 py-3 min-h-0">
        <DataView c={c} data={data} err={err} />
      </div>
      {overflow > 24 && (
        <button
          onClick={(e) => { e.stopPropagation(); onPopOut(); }}
          onMouseDown={(e) => e.stopPropagation()}
          className="absolute bottom-1.5 right-2 z-10 inline-flex items-center gap-1 h-[22px] px-2 rounded-full bg-surface-2/95 border border-line-2 text-[10.5px] text-ink-3 hover:text-ink hover:border-ink-3 cursor-pointer backdrop-blur-sm"
          title="Pop out to see everything — or resize the card">
          <Maximize2 size={10} /> more
        </button>
      )}
    </div>
  );
}

function CtxItem({ children, onSelect, destructive }: any) {
  return (
    <ContextMenu.Item
      onSelect={onSelect}
      className={`flex items-center gap-2.5 px-2.5 py-[7px] rounded-md cursor-pointer outline-none data-[highlighted]:bg-[#26334f] ${destructive ? "text-red" : "text-ink-2"}`}
    >
      {children}
    </ContextMenu.Item>
  );
}

function CtxLabel({ children }: any) {
  return <div className="px-3 pt-1.5 pb-1 text-[10.5px] font-semibold uppercase tracking-wider text-ink-3 truncate">{children}</div>;
}

/* Shared scoped-chat engine */
function useScopedChat(c: Component, onChanged: () => void) {
  const [msgs, setMsgs] = useState<{ who: string; text: string }[]>([
    { who: "hermes", text: "Scoped to this component — ask about its data or tell me to change it." },
  ]);
  const [busy, setBusy] = useState(false);
  const send = async (text: string) => {
    if (!text.trim() || busy) return;
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
  return { msgs, busy, send };
}

function ChatLog({ msgs, busy, size = "sm" }: { msgs: { who: string; text: string }[]; busy: boolean; size?: "sm" | "lg" }) {
  return (
    <>
      {msgs.map((m, i) => (
        <div key={i} className={`${size === "lg" ? "text-[13.5px]" : "text-[13px]"} leading-relaxed`}>
          <span className={`block text-[10px] font-bold uppercase tracking-wider mb-0.5 ${m.who === "you" ? "text-blue-2" : "text-ink-3"}`}>{m.who}</span>
          {m.who === "you"
            ? <span className="text-ink-2 whitespace-pre-wrap">{m.text}</span>
            : <Prose size={size === "lg" ? "md" : "sm"}>{m.text}</Prose>}
        </div>
      ))}
      {busy && <LoaderCircle size={15} className="spin text-ink-3" />}
    </>
  );
}

function ChatInput({ onSend, autoFocus }: { onSend: (t: string) => void; autoFocus?: boolean }) {
  const [input, setInput] = useState("");
  const go = () => { onSend(input); setInput(""); };
  return (
    <div className="flex items-center border-t border-line shrink-0">
      <input
        autoFocus={autoFocus}
        value={input}
        onChange={(e) => setInput(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && go()}
        placeholder="Ask or change this component…"
        className="flex-1 bg-transparent px-3.5 py-3 text-[13px] outline-none placeholder:text-ink-4"
      />
      <Button variant="ghost" size="icon" className="mr-2" onClick={go}><Send size={14} /></Button>
    </div>
  );
}

/* In-card overlay chat */
function ComponentChat({ c, onClose, onChanged }: { c: Component; onClose: () => void; onChanged: () => void }) {
  const { msgs, busy, send } = useScopedChat(c, onChanged);
  return (
    <div className="absolute inset-0 z-20 bg-panel/97 backdrop-blur-sm flex flex-col rounded-[10px] border border-blue/50"
         onMouseDown={(e) => e.stopPropagation()}>
      <div className="flex items-center justify-between px-3.5 h-10 border-b border-line shrink-0">
        <span className="text-[13px] w590 text-blue-2 truncate">◎ {c.title}</span>
        <Button variant="ghost" size="icon" onClick={onClose}>✕</Button>
      </div>
      <div className="flex-1 overflow-auto px-3.5 py-2.5 space-y-2.5 min-h-0">
        <ChatLog msgs={msgs} busy={busy} />
      </div>
      <ChatInput onSend={send} autoFocus />
    </div>
  );
}

/* Dialog chat tab */
function MaximizedChat({ c, onChanged }: { c: Component; onChanged: () => void }) {
  const { msgs, busy, send } = useScopedChat(c, onChanged);
  return (
    <div className="flex-1 flex flex-col min-h-0">
      <div className="flex-1 overflow-auto px-5 py-4 space-y-3 min-h-0">
        <ChatLog msgs={msgs} busy={busy} size="lg" />
      </div>
      <ChatInput onSend={send} autoFocus />
    </div>
  );
}
