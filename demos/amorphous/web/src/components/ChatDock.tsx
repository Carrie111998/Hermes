/* Main dock chat — the invariant full-agent surface. */
import { useEffect, useRef, useState } from "react";
import { Send, PanelRight, PanelBottom, Minus, LoaderCircle } from "lucide-react";

export interface ChatMsg { who: "you" | "hermes" | "tool"; text: string; }

interface Props {
  position: "bottom" | "right";
  collapsed: boolean;
  msgs: ChatMsg[];
  onSend: (text: string) => Promise<void>;
  onMove: () => void;
  onCollapse: () => void;
  busy: boolean;
}

export default function ChatDock({ position, collapsed, msgs, onSend, onMove, onCollapse, busy }: Props) {
  const [input, setInput] = useState("");
  const logRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    logRef.current?.scrollTo({ top: logRef.current.scrollHeight });
  }, [msgs, busy]);

  const send = async () => {
    const t = input.trim();
    if (!t || busy) return;
    setInput("");
    await onSend(t);
  };

  const shell =
    position === "right"
      ? "fixed right-0 top-14 bottom-0 w-[410px] border-l border-line"
      : `fixed left-1/2 -translate-x-1/2 bottom-3 w-[min(860px,calc(100vw-16px))] rounded-2xl border border-line ${collapsed ? "h-12" : "h-[240px]"}`;

  return (
    <div className={`${shell} z-50 bg-panel/95 backdrop-blur-xl flex flex-col shadow-[0_-8px_40px_rgba(0,0,0,.45),0_0_0_1px_rgba(255,255,255,.03)]`}>
      <div className="flex items-center gap-3 h-12 px-4 border-b border-line shrink-0">
        <span className="text-[13.5px] w510 text-ink">◎ Hermes</span>
        <span className="text-[12px] text-ink-3 flex-1 truncate">
          full agent · edits the board live · /rebuild · /evolve
        </span>
        <button onClick={onMove} title="Dock right / bottom" className="text-ink-3 hover:text-ink">
          {position === "bottom" ? <PanelRight size={15} /> : <PanelBottom size={15} />}
        </button>
        <button onClick={onCollapse} title="Collapse" className="text-ink-3 hover:text-ink">
          <Minus size={15} />
        </button>
      </div>
      {!collapsed && (
        <>
          <div ref={logRef} className="flex-1 overflow-auto px-4 py-3 space-y-3 min-h-0">
            {msgs.map((m, i) =>
              m.who === "tool" ? (
                <div key={i} className="text-[12px] font-mono text-ink-3">⚙ {m.text}</div>
              ) : (
                <div key={i} className="text-[13.5px] leading-relaxed">
                  <span className={`block text-[10.5px] font-bold uppercase tracking-wider mb-0.5 ${m.who === "you" ? "text-accent-2" : "text-ink-3"}`}>
                    {m.who}
                  </span>
                  <span className="text-ink whitespace-pre-wrap">{m.text}</span>
                </div>
              )
            )}
            {busy && (
              <div className="flex items-center gap-2 text-[13px] text-ink-3">
                <LoaderCircle size={14} className="spin" /> working…
              </div>
            )}
          </div>
          <div className="flex items-center border-t border-line shrink-0">
            <input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && send()}
              placeholder="Ask Hermes, or tell it to reshape your dashboard…"
              className="flex-1 bg-transparent px-4 py-3.5 text-[14px] outline-none placeholder:text-ink-4"
            />
            <button
              onClick={send}
              disabled={busy}
              className="mr-3 inline-flex items-center gap-1.5 h-8 px-3.5 rounded-lg bg-brand text-white text-[13px] w510 hover:bg-accent-2 disabled:opacity-50 transition-colors"
            >
              <Send size={13} /> Send
            </button>
          </div>
        </>
      )}
    </div>
  );
}
