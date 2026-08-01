/* Onboarding: template gallery, live connection scan, free-form brief. */
import { useEffect, useState } from "react";
import { Check, LoaderCircle } from "lucide-react";
import { api, post, USER } from "./lib/api";

export default function Onboarding({ onDone }: { onDone: () => Promise<any> }) {
  const [opts, setOpts] = useState<any>(null);
  const [template, setTemplate] = useState("developer");
  const [repo, setRepo] = useState("");
  const [brief, setBrief] = useState("");
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState("");

  useEffect(() => {
    api("/api/onboarding/options").then(setOpts).catch((e) => setStatus(String(e)));
  }, []);

  const go = async () => {
    setBusy(true);
    setStatus(brief.trim()
      ? "Building your board, then Hermes is customizing it for your brief (can take a minute)…"
      : "Building your board…");
    try {
      await post("/api/onboarding/complete", { user_id: USER, template, repo: repo.trim(), brief: brief.trim() });
      await onDone();
    } catch (e: any) {
      setStatus(`⚠ ${e.message.slice(0, 200)}`);
      setBusy(false);
    }
  };

  if (!opts) return (
    <div className="h-screen flex items-center justify-center">
      <LoaderCircle size={22} className="spin text-ink-3" />
    </div>
  );

  return (
    <div className="max-w-[720px] mx-auto px-6 pt-16 pb-32">
      <h1 className="text-[26px] font-bold tracking-tight m-0 mb-1.5">
        <span className="text-accent-2">☤</span> Hermes Station
      </h1>
      <p className="text-ink-2 text-[14.5px] leading-relaxed mb-9">
        Your work surface, powered and shaped by Hermes. Pick a starting point, see what's
        already connected on this machine, and optionally tell Hermes what your day looks
        like — it will tailor the board before you ever see it. Everything evolves from
        real usage after that.
      </p>

      <StepLabel>1 · Starting point</StepLabel>
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
        {opts.templates.map((t: any) => (
          <button key={t.id} onClick={() => setTemplate(t.id)}
                  className={`text-left p-4 rounded-xl border transition-colors cursor-pointer bg-white/[0.02]
                    ${template === t.id ? "border-brand ring-1 ring-brand/60" : "border-line hover:border-line-2"}`}>
            <div className="flex items-center justify-between">
              <span className="font-semibold text-[14.5px]">{t.name}</span>
              {template === t.id && <Check size={15} className="text-accent-2" />}
            </div>
            <div className="text-ink-3 text-[12.5px] leading-snug mt-1">{t.blurb}</div>
          </button>
        ))}
      </div>
      {template === "developer" && (
        <input value={repo} onChange={(e) => setRepo(e.target.value)}
               placeholder="Path to your main repo (default: ~/.hermes/hermes-agent)"
               className="mt-2.5 w-full h-10 px-3.5 bg-white/[0.03] border border-line rounded-lg text-[13.5px] text-ink placeholder:text-ink-4 outline-none focus:border-line-2" />
      )}

      <StepLabel>2 · Live connections detected</StepLabel>
      <div className="bg-white/[0.02] border border-line rounded-xl px-4 py-1">
        {opts.connections.map((c: any) => (
          <div key={c.id} className="flex items-center justify-between py-2.5 border-b border-line last:border-0 text-[13.5px]">
            <span>
              {c.name}
              <span className="text-ink-3 text-[12px]"> — {c.detail}</span>
            </span>
            <span className={`text-[10.5px] font-semibold px-2.5 py-1 rounded-full shrink-0 ${c.connected ? "bg-green/15 text-green" : "bg-ink-3/15 text-ink-2"}`}>
              {c.connected ? "connected" : "not set up"}
            </span>
          </div>
        ))}
      </div>

      <StepLabel>3 · Tell Hermes about your day <span className="normal-case font-normal text-ink-3">(optional, free-form)</span></StepLabel>
      <textarea value={brief} onChange={(e) => setBrief(e.target.value)}
                placeholder="e.g. I lead dev on hermes-agent — I care about open PRs, CI, what the community is saying, and BTC. Mornings start with triage."
                className="w-full min-h-[88px] px-4 py-3 bg-white/[0.03] border border-line rounded-lg text-[13.5px] leading-relaxed text-ink placeholder:text-ink-4 outline-none focus:border-line-2 resize-y" />

      <div className="mt-8 flex items-center gap-4">
        <button onClick={go} disabled={busy}
                className="h-11 px-6 rounded-xl bg-brand text-white text-[14.5px] w510 hover:bg-accent-2 transition-colors disabled:opacity-60 inline-flex items-center gap-2">
          {busy && <LoaderCircle size={15} className="spin" />}
          Build my Station
        </button>
        <span className="text-ink-3 text-[13px]">{status}</span>
      </div>
    </div>
  );
}

function StepLabel({ children }: any) {
  return <div className="text-[11px] font-bold uppercase tracking-wider text-ink-3 mt-8 mb-2.5">{children}</div>;
}
