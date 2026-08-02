/* ⌘K command palette — jump to cards, run workflows, station actions,
   or free-text straight to Hermes. Radix Dialog + simple fuzzy filter. */
import { useEffect, useMemo, useRef, useState } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import {
  Search, Play, MessageSquare, Eye, FlaskConical, CornerDownLeft,
  LayoutGrid, EyeOff,
} from "lucide-react";
import { track, type Component, type StationState } from "../lib/api";

export interface PaletteAction {
  id: string;
  label: string;
  hint?: string;
  icon: React.ReactNode;
  run: () => void;
}

interface Props {
  state: StationState;
  onAsk: (text: string) => void;
  onRunWorkflow: (id: string) => void;
  onFocusCard: (id: string) => void;
  onHideCard: (id: string) => void;
  onEvolve: () => void;
  onProposals: () => void;
}

export default function CommandPalette(p: Props) {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
        setQ("");
        setSel(0);
      }
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const actions = useMemo<PaletteAction[]>(() => {
    const acts: PaletteAction[] = [];
    for (const w of p.state.workflows || []) {
      acts.push({
        id: `wf-${w.id}`, label: w.name, hint: "Run workflow",
        icon: <Play size={14} className="text-blue-2" />,
        run: () => p.onRunWorkflow(w.id),
      });
    }
    for (const c of (p.state.layout.components || []) as Component[]) {
      if (c.hidden) {
        acts.push({
          id: `show-${c.id}`, label: `Show: ${c.title}`, hint: "Unhide card",
          icon: <Eye size={14} className="text-ink-3" />,
          run: () => p.onFocusCard(c.id),
        });
      } else {
        acts.push({
          id: `go-${c.id}`, label: c.title, hint: "Jump to card",
          icon: <LayoutGrid size={14} className="text-ink-3" />,
          run: () => p.onFocusCard(c.id),
        });
        acts.push({
          id: `hide-${c.id}`, label: `Hide: ${c.title}`, hint: "Hide card",
          icon: <EyeOff size={14} className="text-ink-4" />,
          run: () => p.onHideCard(c.id),
        });
      }
    }
    acts.push(
      { id: "evolve", label: "Run evolution curator", hint: "⚗ station",
        icon: <FlaskConical size={14} className="text-blue-2" />, run: p.onEvolve },
      { id: "proposals", label: "Open proposals tray", hint: "station",
        icon: <Eye size={14} className="text-ink-3" />, run: p.onProposals },
    );
    return acts;
  }, [p.state]);

  const filtered = useMemo(() => {
    if (!q.trim()) return actions.slice(0, 10);
    const terms = q.toLowerCase().split(/\s+/);
    return actions
      .filter((a) => terms.every((t) => a.label.toLowerCase().includes(t) || (a.hint || "").toLowerCase().includes(t)))
      .slice(0, 10);
  }, [q, actions]);

  const askFallback = q.trim().length > 0;

  const runSel = () => {
    const list = filtered;
    if (sel < list.length) {
      track("palette_action", null, { id: list[sel].id });
      list[sel].run();
    } else if (askFallback) {
      track("palette_ask", null, { text: q });
      p.onAsk(q);
    }
    setOpen(false);
  };

  const total = filtered.length + (askFallback ? 1 : 0);

  return (
    <DialogPrimitive.Root open={open} onOpenChange={setOpen}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className="fixed inset-0 z-[140] bg-[#05080f]/60 backdrop-blur-[2px]" />
        <DialogPrimitive.Content
          className="fixed left-1/2 top-[18%] z-[141] -translate-x-1/2 w-[min(560px,92vw)] bg-panel border border-line-2 rounded-xl overflow-hidden shadow-[0_28px_80px_rgba(2,6,23,.8)]"
          onOpenAutoFocus={(e) => { e.preventDefault(); inputRef.current?.focus(); }}>
          <DialogPrimitive.Title className="sr-only">Command palette</DialogPrimitive.Title>
          <div className="flex items-center gap-2.5 px-4 h-12 border-b border-line">
            <Search size={15} className="text-ink-4" />
            <input
              ref={inputRef}
              value={q}
              onChange={(e) => { setQ(e.target.value); setSel(0); }}
              onKeyDown={(e) => {
                if (e.key === "ArrowDown") { e.preventDefault(); setSel((s) => Math.min(s + 1, total - 1)); }
                if (e.key === "ArrowUp") { e.preventDefault(); setSel((s) => Math.max(s - 1, 0)); }
                if (e.key === "Enter") runSel();
              }}
              placeholder="Search cards, workflows… or just ask Hermes"
              className="flex-1 bg-transparent text-[14px] outline-none placeholder:text-ink-4"
            />
            <kbd className="text-[10px] text-ink-4 border border-line rounded px-1.5 py-0.5">esc</kbd>
          </div>
          <div className="max-h-[320px] overflow-auto p-1.5">
            {filtered.map((a, i) => (
              <button key={a.id}
                      onMouseEnter={() => setSel(i)}
                      onClick={() => { setSel(i); runSel(); }}
                      className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left text-[13.5px] cursor-pointer
                        ${sel === i ? "bg-blue/12 text-ink" : "text-ink-2"}`}>
                {a.icon}
                <span className="flex-1 truncate">{a.label}</span>
                <span className="text-[10.5px] uppercase tracking-wider text-ink-4">{a.hint}</span>
              </button>
            ))}
            {askFallback && (
              <button onMouseEnter={() => setSel(filtered.length)}
                      onClick={runSel}
                      className={`w-full flex items-center gap-3 px-3 py-2 rounded-lg text-left text-[13.5px] cursor-pointer
                        ${sel === filtered.length ? "bg-blue/12 text-ink" : "text-ink-2"}`}>
                <MessageSquare size={14} className="text-blue-2" />
                <span className="flex-1 truncate">Ask Hermes: “{q}”</span>
                <CornerDownLeft size={13} className="text-ink-4" />
              </button>
            )}
            {!filtered.length && !askFallback && (
              <div className="px-3 py-6 text-center text-[12.5px] text-ink-4">Type to search…</div>
            )}
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
