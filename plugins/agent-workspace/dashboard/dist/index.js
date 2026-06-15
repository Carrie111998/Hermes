/**
 * Agent Workspace — Dashboard Plugin (frontend)
 *
 * A FRIDAY-OS surface inside the Hermes dashboard, beside the Kanban tab:
 *   - Agent Lanes  — the FRIDAY-OS agent roster (mirrors BOOT.md "## Lanes").
 *   - Learnings    — the aux model: live grounding-store census + semantic
 *                    recall, fetched from this plugin's backend
 *                    (/api/plugins/agent-workspace/learnings) through the
 *                    dashboard's own auth (SDK.fetchJSON carries the token).
 *
 * Plain IIFE, no build step — uses window.__HERMES_PLUGIN_SDK__ for React +
 * DS components, matching the kanban plugin. Only utility classes the host
 * dashboard is known to compile are used; everything visual leans on DS
 * components so it reskins with the active theme.
 */
(function () {
  "use strict";

  var SDK = window.__HERMES_PLUGIN_SDK__;
  if (!SDK) return;

  var React = SDK.React;
  var h = React.createElement;
  var C = SDK.components;
  var useState = SDK.hooks.useState;
  var useEffect = SDK.hooks.useEffect;
  var useCallback = SDK.hooks.useCallback;

  var API = "/api/plugins/agent-workspace";

  // FRIDAY-OS agent roster — mirrors the BOOT.md "## Lanes" table. Display
  // chrome (the roster changes rarely); the Learnings panel below is the live
  // data. TODO(backend): serve this from the kernel (friday://boot) so it can't
  // drift from BOOT.md.
  var LANES = [
    { agent: "atlas", role: "Systems architect", status: "active" },
    { agent: "claude", role: "Workspace dev agent", status: "active" },
    { agent: "fridai", role: "OS orchestrator", status: "active" },
    { agent: "librarian", role: "Memory synthesizer", status: "active" },
    { agent: "neo", role: "Security & memory-integrity gate", status: "active" },
    { agent: "orion", role: "Research analyst", status: "active" },
    { agent: "validator", role: "Independent verifier", status: "active" },
    { agent: "vector", role: "Senior engineer", status: "active" },
    { agent: "venus", role: "PA & broadcaster", status: "active" },
    { agent: "hermes", role: "Network & messaging router", status: "alongside" },
    { agent: "openclaw", role: "Platform coordinator (dropped)", status: "dropped" }
  ];

  function statusVariant(s) {
    if (s === "active") return "default";
    if (s === "alongside") return "secondary";
    return "outline";
  }

  function LaneRow(l) {
    return h("div", { key: l.agent, className: "flex items-center justify-between gap-2 py-1" },
      h("div", { className: "flex flex-col" },
        h("span", { className: "text-sm font-mono-ui" }, l.agent),
        h("span", { className: "text-xs text-muted-foreground" }, l.role)),
      h(C.Badge, { variant: statusVariant(l.status) }, l.status));
  }

  function LanesCard() {
    return h(C.Card, { className: "agent-workspace-card" },
      h(C.CardHeader, null, h(C.CardTitle, null, "Agent Lanes")),
      h(C.CardContent, null,
        h("div", { className: "flex flex-col gap-1" }, LANES.map(LaneRow))));
  }

  function Stat(n, label) {
    return h("div", { className: "flex flex-col" },
      h("span", { className: "text-lg font-mono-ui" }, String(n)),
      h("span", { className: "text-xs text-muted-foreground" }, label));
  }

  function FeedRow(row) {
    return h("div", { key: row.id, className: "flex flex-col gap-1 py-2" },
      h("span", { className: "text-sm" }, row.statement),
      h("div", { className: "flex items-center gap-2" },
        h(C.Badge, { variant: "outline" }, row.kind),
        typeof row.confidence === "number"
          ? h("span", { className: "text-xs text-muted-foreground" }, "conf " + row.confidence.toFixed(2))
          : null));
  }

  function LearningsCard() {
    var st = useState({ status: "loading", stats: null, feed: [], error: null });
    var state = st[0], setState = st[1];

    var load = useCallback(function () {
      setState(function (p) { return Object.assign({}, p, { status: "loading", error: null }); });
      SDK.fetchJSON(API + "/learnings?limit=8")
        .then(function (d) {
          setState({ status: "live", stats: d.stats || null, feed: d.feed || [], error: null });
        })
        .catch(function (e) {
          setState({ status: "error", stats: null, feed: [], error: String((e && e.message) || e) });
        });
    }, []);

    useEffect(function () { load(); }, [load]);

    var byKind = (state.stats && state.stats.by_kind) || null;
    var active = (state.stats && state.stats.by_status && state.stats.by_status.active) || 0;

    return h(C.Card, { className: "agent-workspace-card" },
      h(C.CardHeader, { className: "flex flex-row items-center justify-between" },
        h(C.CardTitle, null, "Learnings"),
        h(C.Button, { variant: "outline", size: "sm", onClick: load, disabled: state.status === "loading" }, "refresh")),
      h(C.CardContent, null,
        h("div", { className: "flex flex-col gap-3" },
          state.status === "loading" ? h("div", { className: "text-xs text-muted-foreground" }, "loading…") : null,
          state.status === "error"
            ? h("div", { className: "text-xs text-destructive" }, "kernel unreachable: " + state.error)
            : null,
          state.stats
            ? h("div", { className: "flex gap-6" },
                Stat(state.stats.total || 0, "total"),
                Stat(active, "active"),
                Stat(state.stats.edges || 0, "edges"))
            : null,
          byKind
            ? h("div", { className: "flex flex-wrap gap-1" },
                Object.keys(byKind).map(function (k) {
                  return h(C.Badge, { key: k, variant: "secondary" }, k + " " + byKind[k]);
                }))
            : null,
          state.status === "live" && state.feed.length === 0
            ? h("div", { className: "text-xs text-muted-foreground" }, "no learnings recalled")
            : null,
          h("div", { className: "flex flex-col" }, state.feed.map(FeedRow)))));
  }

  function Workspace() {
    return h("div", { className: "agent-workspace flex flex-col gap-4 p-4" },
      h(LanesCard),
      h(LearningsCard));
  }

  window.__HERMES_PLUGINS__.register("agent-workspace", Workspace);
})();
