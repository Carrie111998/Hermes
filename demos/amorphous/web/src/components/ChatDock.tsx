/* Main dock chat — the invariant full-agent surface.
   Three modes: bottom console (structural), right rail (fixed), and a
   POP-OUT floating window (draggable + resizable, always on top). */
import { useEffect, useRef, useState } from "react";
import {
  Send, PanelRight, PanelBottom, Minus, LoaderCircle, PictureInPicture2, GripHorizontal,
} from "lucide-react";
import { Button, Tip } from "./ui";
import Prose from "./Prose";

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

export default function ChatDock(props: Props) {
  const [popped, setPopped] = useState(false);
  if (popped) return <FloatingChat {...props} onDock={() => setPopped(false)} />;
  return <DockedChat {...props} onPop={() => setPopped(true)} />;
}

/* ---------------- docked variants ---------------- */
function DockedChat({ position, collapsed, msgs, onSend, onMove, onCollapse, busy, onPop }:
  Props & { onPop: () => void }) {
  const shell =
    position === "right"
      ? "fixed right-0 top-14 bottom-0 w-[410px] border-l border-line z-50 bg-panel/95 backdrop-blur-xl shadow-[0_-8px_40px_rgba(0,0,0,.45)]"
      : `shrink-0 w-full border-t border-line bg-panel ${collapsed ? "h-12" : "h-[236px]"}`;

  return (
    <div className={`${shell} flex flex-col transition-[height] duration-150`}>
      <Head onMove={onMove} onCollapse={onCollapse} onPop={onPop} position={position} />
      {!collapsed && <Body msgs={msgs} busy={busy} onSend={onSend} />}
    </div>
  );
}

/* ---------------- floating window ---------------- */
function FloatingChat(props: Props & { onDock: () => void }) {
  const [pos, setPos] = useState(() => ({
    x: Math.max(20, window.innerWidth - 500),
    y: Math.max(20, window.innerHeight - 480),
  }));
  const [size, setSize] = useState({ w: 460, h: 420 });
  const drag = useRef<{ dx: number; dy: number } | null>(null);

  useEffect(() => {
    const onMoveEv = (e: MouseEvent) => {
      if (!drag.current) return;
      setPos({
        x: Math.min(Math.max(8, e.clientX - drag.current.dx), window.innerWidth - 120),
        y: Math.min(Math.max(8, e.clientY - drag.current.dy), window.innerHeight - 60),
      });
    };
    const onUp = () => (drag.current = null);
    window.addEventListener("mousemove", onMoveEv);
    window.addEventListener("mouseup", onUp);
    return () => { window.removeEventListener("mousemove", onMoveEv); window.removeEventListener("mouseup", onUp); };
  }, []);

  return (
    <div className="fixed z-[125] flex flex-col bg-panel border border-line-2 rounded-xl overflow-hidden shadow-[0_28px_80px_rgba(2,6,23,.75),0_0_0_1px_rgba(255,255,255,.04)]"
         style={{ left: pos.x, top: pos.y, width: size.w, height: size.h, resize: "both" }}>
      <div
        className="flex items-center gap-2 h-11 px-3 border-b border-line shrink-0 cursor-grab active:cursor-grabbing select-none"
        onMouseDown={(e) => { drag.current = { dx: e.clientX - pos.x, dy: e.clientY - pos.y }; }}>
        <GripHorizontal size={13} className="text-ink-4" />
        <span className="text-[13px] w510">◎ Hermes</span>
        <span className="text-[11px] text-ink-4 flex-1 truncate">floating — drag me anywhere</span>
        <Tip label="Dock back">
          <Button variant="ghost" size="icon" onClick={props.onDock}><PanelBottom size={14} /></Button>
        </Tip>
      </div>
      <Body msgs={props.msgs} busy={props.busy} onSend={props.onSend} />
      {/* resize handle (native CSS resize needs overflow) */}
      <div className="absolute bottom-0 right-0 w-4 h-4 cursor-nwse-resize"
           onMouseDown={(e) => {
             e.preventDefault();
             const sx = e.clientX, sy = e.clientY, sw = size.w, sh = size.h;
             const mm = (ev: MouseEvent) =>
               setSize({ w: Math.max(340, sw + ev.clientX - sx), h: Math.max(260, sh + ev.clientY - sy) });
             const mu = () => { window.removeEventListener("mousemove", mm); window.removeEventListener("mouseup", mu); };
             window.addEventListener("mousemove", mm);
             window.addEventListener("mouseup", mu);
           }}>
        <svg viewBox="0 0 16 16" className="w-4 h-4 text-ink-4"><path d="M14 8 L8 14 M14 12 L12 14" stroke="currentColor" strokeWidth="1.5" fill="none" /></svg>
      </div>
    </div>
  );
}

/* ---------------- shared pieces ---------------- */
function Head({ onMove, onCollapse, onPop, position }:
  { onMove: () => void; onCollapse: () => void; onPop: () => void; position: string }) {
  return (
    <div className="flex items-center gap-3 h-12 px-4 border-b border-line shrink-0">
      <span className="text-[13.5px] w510 text-ink">◎ Hermes</span>
      <span className="text-[12px] text-ink-3 flex-1 truncate">
        full agent · edits the board live · /rebuild · /evolve
      </span>
      <Tip label="Pop out into a window">
        <Button variant="ghost" size="icon" onClick={onPop}><PictureInPicture2 size={14} /></Button>
      </Tip>
      <Tip label={position === "bottom" ? "Dock right" : "Dock bottom"}>
        <Button variant="ghost" size="icon" onClick={onMove}>
          {position === "bottom" ? <PanelRight size={14} /> : <PanelBottom size={14} />}
        </Button>
      </Tip>
      <Tip label="Collapse">
        <Button variant="ghost" size="icon" onClick={onCollapse}><Minus size={14} /></Button>
      </Tip>
    </div>
  );
}

function Body({ msgs, busy, onSend }: { msgs: ChatMsg[]; busy: boolean; onSend: (t: string) => Promise<void> }) {
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

  return (
    <>
      <div ref={logRef} className="flex-1 overflow-auto px-4 py-3 space-y-3 min-h-0">
        {msgs.map((m, i) =>
          m.who === "tool" ? (
            <div key={i} className="text-[12px] font-mono text-ink-4">⚙ {m.text}</div>
          ) : (
            <div key={i} className="text-[13.5px] leading-relaxed">
              <span className={`block text-[10.5px] font-bold uppercase tracking-wider mb-0.5 ${m.who === "you" ? "text-blue-2" : "text-ink-3"}`}>
                {m.who}
              </span>
              {m.who === "you"
                ? <span className="text-ink whitespace-pre-wrap">{m.text}</span>
                : <Prose size="md">{m.text}</Prose>}
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
        <Button className="mr-3" onClick={send} disabled={busy}>
          <Send size={13} /> Send
        </Button>
      </div>
    </>
  );
}
