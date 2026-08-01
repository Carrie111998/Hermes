/* Hermes Station frontend.
 * Absolute-positioned skyline grid (zero whitespace), right-click component
 * context menus, per-component agent chat, invariant main dock, proposals,
 * SSE live updates, telemetry. */

const USER = new URLSearchParams(location.search).get("user") || "demo";
let STATE = null;
let PREVIEW = null;
const telemetryQueue = [];
const ROW = 128, GAP = 10;

/* ---------------- utils ---------------- */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function when(ts) {
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.text()).slice(0, 300));
  return r.json();
}
function post(path, body) {
  return api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
}
function toast(msg, ms = 2600) {
  const t = document.getElementById("toast");
  t.textContent = msg; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), ms);
}

/* ---------------- telemetry ---------------- */
function track(type, componentId, payload) {
  telemetryQueue.push({ type, component_id: componentId || null, payload: payload || null });
}
setInterval(async () => {
  if (!telemetryQueue.length) return;
  const events = telemetryQueue.splice(0);
  try { await post("/api/telemetry", { user_id: USER, events }); }
  catch { telemetryQueue.unshift(...events); }
}, 4000);

const dwell = new Map();
function attachDwell(el, cid) {
  el.addEventListener("mouseenter", () => dwell.set(cid, performance.now()));
  el.addEventListener("mouseleave", () => {
    const t0 = dwell.get(cid);
    if (t0) {
      const secs = (performance.now() - t0) / 1000;
      if (secs > 0.8) track("focus_dwell", cid, { seconds: Math.round(secs * 10) / 10 });
      dwell.delete(cid);
    }
  });
}

/* ---------------- state ---------------- */
async function loadState() {
  STATE = await api(`/api/state?user_id=${USER}`);
  if (!STATE.onboarded) { location.href = `/onboarding?user=${USER}`; return; }
  renderAll();
}
function activeSpec() { return PREVIEW ? PREVIEW.spec : STATE.layout; }

function renderAll() {
  const label = document.getElementById("agent-label");
  label.textContent = STATE.agent.model || "agent";
  document.getElementById("agent-badge").classList.add("live");
  renderGrid();
  renderProposals();
  renderPreviewBanner();
  const dock = document.getElementById("chat-dock");
  dock.dataset.position = (activeSpec().chat_dock || {}).position || "bottom";
  syncDockSpace();
}

/* Reserve layout space for the dock so it NEVER covers data. */
function syncDockSpace() {
  const dock = document.getElementById("chat-dock");
  const root = document.documentElement.style;
  const collapsed = dock.classList.contains("collapsed");
  if (dock.dataset.position === "right") {
    root.setProperty("--dock-space", "12px");
    root.setProperty("--dock-right-space", "420px");
  } else {
    root.setProperty("--dock-space", collapsed ? "60px" : "244px");
    root.setProperty("--dock-right-space", "0px");
  }
  renderGridSoon();
}
let gridSoonTimer = null;
function renderGridSoon() {
  clearTimeout(gridSoonTimer);
  gridSoonTimer = setTimeout(() => STATE && renderGrid(), 60);
}

/* ---------------- grid ---------------- */
function renderGrid() {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  const spec = activeSpec();
  const comps = (spec.components || []).filter((c) => !c.hidden);
  const colW = () => (grid.clientWidth - 11 * GAP) / 12;
  const cw = colW();
  let maxBottom = 0;
  for (const c of comps) {
    const card = document.createElement("div");
    card.className = "card";
    const x = c.col * (cw + GAP);
    const y = c.row * (ROW + GAP);
    const w = c.w * cw + (c.w - 1) * GAP;
    const h = c.h * ROW + (c.h - 1) * GAP;
    Object.assign(card.style, { left: x + "px", top: y + "px", width: w + "px", height: h + "px" });
    maxBottom = Math.max(maxBottom, y + h);
    card.dataset.cid = c.id;
    card.innerHTML = `
      <div class="card-head">
        <span class="card-title">${esc(c.title)}</span>
        <span class="card-actions">
          <button class="icon-btn a-ask" title="Ask this component">◎</button>
          <button class="icon-btn a-up" title="Move up">↑</button>
          <button class="icon-btn a-hide" title="Hide">—</button>
        </span>
      </div>
      <div class="card-body" id="body-${c.id}"></div>`;
    card.querySelector(".card-body").innerHTML =
      `<div class="skel">${[85, 60, 75, 50, 70, 40].slice(0, Math.min(c.h * 2 + 1, 6))
        .map((wd) => `<div class="bar" style="width:${wd}%"></div>`).join("")}</div>`;
    grid.appendChild(card);
    attachDwell(card, c.id);
    card.addEventListener("click", () => track("click", c.id));
    card.addEventListener("contextmenu", (e) => { e.preventDefault(); openCtxMenu(e, c); });
    if (PREVIEW) {
      card.querySelector(".card-actions").style.display = "none";
    } else {
      card.querySelector(".a-ask").addEventListener("click", (e) => { e.stopPropagation(); openCompChat(c, card); });
      card.querySelector(".a-hide").addEventListener("click", (e) => { e.stopPropagation(); hideComponent(c.id); });
      card.querySelector(".a-up").addEventListener("click", (e) => { e.stopPropagation(); moveComponentUp(c.id); });
    }
    loadComponentData(c);
    track("view", c.id);
  }
  grid.style.height = maxBottom + "px";

  const bar = document.getElementById("hidden-bar");
  const hidden = (spec.components || []).filter((c) => c.hidden);
  bar.innerHTML = "";
  if (hidden.length && !PREVIEW) {
    bar.append("Hidden:");
    for (const c of hidden) {
      const b = document.createElement("button");
      b.className = "btn ghost sm"; b.textContent = `+ ${c.title}`;
      b.addEventListener("click", () => showComponent(c.id));
      bar.appendChild(b);
    }
  }
}
window.addEventListener("resize", () => STATE && renderGrid());

async function loadComponentData(c) {
  const body = document.getElementById(`body-${c.id}`);
  if (!body) return;
  try {
    const pv = PREVIEW ? `&proposal_id=${PREVIEW.proposal.id}` : "";
    const d = await api(`/api/component/${c.id}/data?user_id=${USER}${pv}`);
    body.innerHTML = "";
    body.appendChild(renderData(c, d));
    autoFit(c, body, d);
  } catch (e) {
    body.innerHTML = `<div class="err">${esc(e.message)}</div>`;
  }
}

/* ---- auto-fit: size cards to their content, then re-pack once ---- */
let fitTimer = null;
const fitted = new Set();
function autoFit(c, body, d) {
  if (PREVIEW || fitted.has(c.id)) return;
  if (d.kind === "timeseries") return; // charts fill whatever they get
  const contentH = body.scrollHeight + 40 /*head*/ + 2 /*border*/;
  const needRows = Math.max(1, Math.min(6, Math.ceil((contentH + GAP) / (ROW + GAP))));
  const over = body.scrollHeight > body.clientHeight + 4;           // cut off
  const under = c.h > 1 && body.scrollHeight < (c.h - 1) * ROW * 0.55; // mostly empty
  if ((over && needRows > c.h) || (under && needRows < c.h)) {
    c.h = needRows;
    fitted.add(c.id);
    clearTimeout(fitTimer);
    fitTimer = setTimeout(async () => {
      // one repack + quiet persist for all fitted components this cycle
      await post("/api/layout", { user_id: USER, spec: STATE.layout });
      STATE = await api(`/api/state?user_id=${USER}`);
      renderGrid();
    }, 350);
  } else {
    fitted.add(c.id);
  }
}

function renderData(c, d) {
  const el = document.createElement("div");
  switch (d.kind) {
    case "metric": {
      const up = d.delta > 0;
      el.innerHTML = `<div class="metric-value">${esc(String(d.value))}<span class="metric-unit">${esc(d.unit || "")}</span></div>
        ${d.delta != null ? `<div class="metric-delta ${up ? "delta-up" : "delta-down"}">${up ? "▲" : "▼"} ${Math.abs(d.delta)}</div>` : ""}`;
      break;
    }
    case "kv": {
      el.className = "kv";
      el.innerHTML = d.pairs.map(([k, v]) => `<span class="k">${esc(k)}</span><span class="v">${esc(v)}</span>`).join("");
      break;
    }
    case "timeseries": el.appendChild(sparkline(d.points, d.label)); el.style.height = "100%"; break;
    case "table": {
      const t = document.createElement("table");
      t.className = "data";
      t.innerHTML = `<thead><tr>${d.columns.map((x) => `<th>${esc(x)}</th>`).join("")}</tr></thead>
        <tbody>${d.rows.map((r) => `<tr>${r.map((v) => `<td class="${/^[-+$0-9.,%#]/.test(String(v)) ? "num" : ""}" title="${esc(String(v))}">${esc(String(v))}</td>`).join("")}</tr>`).join("")}</tbody>`;
      el.appendChild(t);
      break;
    }
    case "links": {
      el.className = "links";
      el.innerHTML = d.links.map((l) => `<a href="${esc(l.url)}" target="_blank">${esc(l.title)}</a>`).join("");
      break;
    }
    case "feed": {
      el.innerHTML = d.items.length
        ? d.items.map((i) => `<div class="feed-item"><span class="when">${when(i.when)}</span><span class="txt">${esc(i.icon || "")} ${esc(i.text)}</span></div>`).join("")
        : `<span class="unconnected">No activity yet — run a workflow or chat.</span>`;
      break;
    }
    case "notes": { el.className = "notes-md"; el.textContent = d.markdown; break; }
    case "connections": {
      el.innerHTML = d.connections.map((s) =>
        `<div class="conn-row"><span>${esc(s.name)}</span>
         <span class="conn-state ${s.connected ? "on" : "off"}">${s.connected ? "on" : "off"}</span></div>`).join("");
      break;
    }
    case "workflow": return renderWorkflow(c, d);
    case "unconnected": {
      el.className = "unconnected";
      el.innerHTML = `<b>${esc(d.source)}</b> isn't connected.<br><code>${esc(d.how)}</code>`;
      break;
    }
    default: el.innerHTML = `<div class="err">${esc(d.error || "no data")}</div>`;
  }
  return el;
}

function renderWorkflow(c, d) {
  const el = document.createElement("div");
  if (!d.workflow) { el.innerHTML = `<div class="err">workflow missing</div>`; return el; }
  const inputs = c.props.inputs || [];
  el.innerHTML = `<div class="wf-desc">${esc(d.workflow.description || "")}</div>
    <div class="wf-inputs">${inputs.map((i) => `<input class="input" placeholder="${esc(i.label)}" data-name="${esc(i.name)}">`).join("")}</div>
    <button class="btn primary sm wf-run">▶ Run</button>
    <div class="wf-result" style="display:none"></div>`;
  const btn = el.querySelector(".wf-run"), resBox = el.querySelector(".wf-result");
  if (d.runs && d.runs.length) {
    resBox.style.display = "block";
    resBox.textContent = `Last run ${when(d.runs[0].ts)}:\n${(d.runs[0].result || "").slice(0, 1200)}`;
  }
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const inp = {};
    el.querySelectorAll(".wf-inputs input").forEach((i) => (inp[i.dataset.name] = i.value));
    btn.disabled = true; btn.innerHTML = `<span class="spin">◐</span> Running`;
    resBox.style.display = "block"; resBox.textContent = "Hermes is working (full agent — may take a minute)…";
    try {
      const run = await post(`/api/workflow/${d.workflow.id}/run`, { user_id: USER, inputs: inp });
      resBox.textContent = run.result;
    } catch (err) { resBox.textContent = `⚠ ${err.message}`; }
    btn.disabled = false; btn.textContent = "▶ Run";
  });
  return el;
}

function sparkline(points, label) {
  const w = 600, h = 150, pad = 6;
  const vals = points.map((p) => p[1]);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const xs = points.map((_, i) => pad + (i / Math.max(points.length - 1, 1)) * (w - 2 * pad));
  const ys = vals.map((v) => h - pad - ((v - mn) / (mx - mn || 1)) * (h - 2 * pad - 14));
  const dAttr = xs.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.setAttribute("preserveAspectRatio", "none");
  svg.classList.add("spark");
  const last = vals[vals.length - 1];
  svg.innerHTML = `
    <path d="${dAttr} L${xs[xs.length - 1]},${h} L${xs[0]},${h} Z" fill="rgba(45,212,191,.10)" stroke="none"/>
    <path d="${dAttr}" fill="none" stroke="#2dd4bf" stroke-width="1.8"/>
    <text x="${pad}" y="12" fill="#71717a" font-size="10">${esc(label || "")} · ${mn.toLocaleString()}–${mx.toLocaleString()} · now ${last.toLocaleString()}</text>`;
  return svg;
}

/* ---------------- context menu ---------------- */
let ctxComp = null;
function openCtxMenu(e, c) {
  ctxComp = c;
  const m = document.getElementById("ctx-menu");
  m.innerHTML = `
    <div class="ctx-label">${esc(c.title)}</div>
    <button class="ctx-item" data-act="ask">◎ Ask this component…</button>
    <button class="ctx-item" data-act="refresh">↻ Refresh data</button>
    <div class="ctx-sep"></div>
    <button class="ctx-item" data-act="up">↑ Move to front</button>
    <button class="ctx-item" data-act="wider">⇤⇥ Wider</button>
    <button class="ctx-item" data-act="narrower">⇥⇤ Narrower</button>
    <button class="ctx-item" data-act="taller">↕ Taller</button>
    <button class="ctx-item" data-act="shorter">↔ Shorter</button>
    <div class="ctx-sep"></div>
    <button class="ctx-item" data-act="hide">— Hide</button>
    <button class="ctx-item" data-act="remove" style="color:var(--destructive)">✕ Remove</button>`;
  m.classList.add("open");
  const pad = 8, mw = 220, mh = m.offsetHeight || 320;
  m.style.left = Math.min(e.clientX, innerWidth - mw - pad) + "px";
  m.style.top = Math.min(e.clientY, innerHeight - mh - pad) + "px";
  m.querySelectorAll(".ctx-item").forEach((b) =>
    b.addEventListener("click", () => { closeCtxMenu(); ctxAction(b.dataset.act, c); }));
  track("context_menu", c.id);
}
function closeCtxMenu() { document.getElementById("ctx-menu").classList.remove("open"); }
document.addEventListener("click", closeCtxMenu);
document.addEventListener("scroll", closeCtxMenu, true);

async function ctxAction(act, c) {
  const resize = (dw, dh) => saveMutations([{ op: "resize", component_id: c.id,
    w: Math.max(1, Math.min(12, c.w + dw)), h: Math.max(1, Math.min(6, c.h + dh)) }]);
  switch (act) {
    case "ask": { const card = document.querySelector(`.card[data-cid="${c.id}"]`); openCompChat(c, card); break; }
    case "refresh": loadComponentData(c); toast("Refreshed"); break;
    case "up": moveComponentUp(c.id); break;
    case "wider": await resize(1, 0); break;
    case "narrower": await resize(-1, 0); break;
    case "taller": await resize(0, 1); break;
    case "shorter": await resize(0, -1); break;
    case "hide": hideComponent(c.id); break;
    case "remove":
      if (confirm(`Remove "${c.title}" from the dashboard?`)) {
        track("remove", c.id);
        await saveMutations([{ op: "remove", component_id: c.id }]);
      }
      break;
  }
}

/* ---------------- layout edits ---------------- */
async function saveMutations(muts) {
  // apply locally via server: post full spec after applying is racey; instead use
  // a tiny endpoint-free approach — mutate through /api/layout with edited spec.
  const spec = JSON.parse(JSON.stringify(STATE.layout));
  for (const m of muts) {
    const comp = spec.components.find((x) => x.id === m.component_id);
    if (!comp) continue;
    if (m.op === "resize") { comp.w = m.w; comp.h = m.h; }
    if (m.op === "remove") spec.components = spec.components.filter((x) => x.id !== m.component_id);
    if (m.op === "hide") comp.hidden = true;
    if (m.op === "show") comp.hidden = false;
  }
  await post("/api/layout", { user_id: USER, spec });
  track("move", muts[0] && muts[0].component_id);
  await loadState();
}
function findComp(cid) { return STATE.layout.components.find((c) => c.id === cid); }
async function hideComponent(cid) { track("hide", cid); await saveMutations([{ op: "hide", component_id: cid }]); }
async function showComponent(cid) { await saveMutations([{ op: "show", component_id: cid }]); }
async function moveComponentUp(cid) {
  const spec = JSON.parse(JSON.stringify(STATE.layout));
  const i = spec.components.findIndex((c) => c.id === cid);
  if (i > 0) {
    spec.components.unshift(spec.components.splice(i, 1)[0]);
    track("move", cid);
    await post("/api/layout", { user_id: USER, spec });
    await loadState();
  }
}

/* ---------------- component chat ---------------- */
let compChatTarget = null;
function openCompChat(c, cardEl) {
  compChatTarget = c;
  const box = document.getElementById("comp-chat");
  document.getElementById("comp-chat-title").textContent = c.title;
  document.getElementById("comp-chat-log").innerHTML =
    `<div class="msg agent"><span class="who">hermes · scoped</span>I can answer questions about this component or change it (query, size, title). What do you need?</div>`;
  box.classList.add("open");
  const r = cardEl.getBoundingClientRect();
  const bw = 380, bh = Math.min(460, innerHeight - 40);
  let x = Math.min(r.left, innerWidth - bw - 12);
  let y = r.bottom + 8;
  if (y + 300 > innerHeight) y = Math.max(12, r.top - 310);
  box.style.left = x + "px"; box.style.top = y + "px";
  document.getElementById("comp-chat-input").focus();
  track("component_chat_open", c.id);
}
document.getElementById("comp-chat-close").addEventListener("click", () =>
  document.getElementById("comp-chat").classList.remove("open"));
document.getElementById("comp-chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if (!compChatTarget) return;
  const input = document.getElementById("comp-chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  const log = document.getElementById("comp-chat-log");
  log.insertAdjacentHTML("beforeend", `<div class="msg user"><span class="who">you</span>${esc(text)}</div>`);
  log.insertAdjacentHTML("beforeend", `<div class="msg agent" id="cc-pending"><span class="who">hermes</span><span class="spin">◐</span></div>`);
  log.scrollTop = log.scrollHeight;
  try {
    const r = await post(`/api/component/${compChatTarget.id}/chat`, { user_id: USER, text });
    document.getElementById("cc-pending").innerHTML = `<span class="who">hermes</span>${esc(r.reply)}`;
  } catch (err) {
    document.getElementById("cc-pending").innerHTML = `<span class="who">hermes</span><span class="err">${esc(err.message)}</span>`;
  }
  log.scrollTop = log.scrollHeight;
});

/* ---------------- proposals ---------------- */
function renderProposals() {
  const list = document.getElementById("proposal-list");
  const count = document.getElementById("proposal-count");
  const pending = STATE.proposals || [];
  count.textContent = pending.length;
  count.classList.toggle("zero", pending.length === 0);
  list.innerHTML = pending.length ? "" :
    `<p style="color:var(--muted-2);font-size:13px">No pending proposals. The curator reviews your usage on a schedule (or press ⚗ Evolve).</p>`;
  for (const p of pending) {
    const div = document.createElement("div");
    div.className = "proposal";
    div.innerHTML = `<div class="meta">${esc(p.engine)} · ${when(p.created_at)}</div>
      <div class="summary">${esc(p.summary)}</div>
      <ul>${p.mutations.slice(0, 8).map((m) => `<li>${esc(mutLabel(m))}</li>`).join("")}</ul>
      <div class="rationale">${esc(p.rationale || "")}</div>
      <textarea placeholder="Optional feedback — steers the next evolution"></textarea>
      <div class="actions">
        <button class="btn sm t-try">👁 Try it</button>
        <button class="btn sm primary t-ok">✔ Apply</button>
        <button class="btn sm destructive t-no">✕ Reject</button>
      </div>`;
    const fb = () => div.querySelector("textarea").value;
    div.querySelector(".t-try").addEventListener("click", async () => {
      const pv = await api(`/api/proposal/${p.id}/preview`);
      PREVIEW = { proposal: p, spec: pv.preview, diff: pv.diff };
      document.getElementById("proposal-tray").classList.remove("open");
      renderAll(); scrollTo({ top: 0, behavior: "smooth" });
    });
    div.querySelector(".t-ok").addEventListener("click", () => actProposal(p.id, "approve", fb(), "up"));
    div.querySelector(".t-no").addEventListener("click", () => actProposal(p.id, "reject", fb(), "down"));
    list.appendChild(div);
  }
}
function mutLabel(m) {
  const t = {
    promote: `Promote ${m.component_id}`, shrink: `Shrink ${m.component_id}`,
    resize: `Resize ${m.component_id}`, hide: `Hide ${m.component_id}`,
    show: `Show ${m.component_id}`, remove: `Remove ${m.component_id}`,
    retitle: `Rename ${m.component_id} → "${m.title}"`,
    add: `Add ${m.component ? m.component.type : ""}: "${m.component ? m.component.title : ""}"`,
    set_props: `Reconfigure ${m.component_id}`, set_notes: "Refresh briefing",
    replace_spec: `Full rebuild (${m.spec && m.spec.components ? m.spec.components.length : 0} components)`,
    move_chat_dock: `Chat dock → ${m.position}`,
  };
  return t[m.op] || m.op;
}
async function actProposal(pid, action, feedback, sentiment) {
  await post(`/api/proposal/${pid}`, { action, feedback, sentiment });
  PREVIEW = null;
  await loadState();
  toast(action === "approve" ? "Applied — dashboard updated" : "Rejected — the curator will steer away from this");
}

/* ---------------- preview banner ---------------- */
function renderPreviewBanner() {
  let bar = document.getElementById("preview-banner");
  if (!PREVIEW) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "preview-banner";
    document.body.insertBefore(bar, document.getElementById("grid-wrap"));
  }
  const diffTxt = PREVIEW.diff.map((d) => `${d.change}: ${d.title}`).join(" · ") || "reflow only";
  bar.innerHTML = `<b>Previewing proposal</b><span class="diff">${esc(diffTxt)}</span>
    <button class="btn sm primary" id="pv-ok">✔ Keep</button>
    <button class="btn sm" id="pv-back">✕ Go back</button>`;
  bar.querySelector("#pv-ok").addEventListener("click", () => actProposal(PREVIEW.proposal.id, "approve", "", "up"));
  bar.querySelector("#pv-back").addEventListener("click", () => {
    const why = prompt("Why keep the current layout? (optional — steers the curator)") || "";
    actProposal(PREVIEW.proposal.id, "reject", why, "down");
  });
}

/* ---------------- main chat ---------------- */
function addMsg(who, text) {
  const log = document.getElementById("chat-log");
  const div = document.createElement("div");
  div.className = `msg ${who}`;
  div.innerHTML = `<span class="who">${who === "user" ? "you" : "hermes"}</span>${esc(text)}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
  return div;
}
function addToolMsg(name) {
  const log = document.getElementById("chat-log");
  const div = document.createElement("div");
  div.className = "msg tool";
  div.textContent = `⚙ ${name}`;
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}
document.getElementById("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("chat-input");
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  addMsg("user", text);
  const pend = addMsg("agent", "");
  pend.innerHTML = `<span class="who">hermes</span><span class="spin">◐</span> working…`;
  try {
    const r = await post("/api/chat", { user_id: USER, text });
    pend.innerHTML = `<span class="who">hermes</span>${esc(r.reply)}`;
    if (r.proposal) { await loadState(); document.getElementById("proposal-tray").classList.add("open"); }
  } catch (err) {
    pend.innerHTML = `<span class="who">hermes</span><span class="err">${esc(err.message)}</span>`;
  }
});
document.getElementById("chat-dock-hide").addEventListener("click", () => {
  document.getElementById("chat-dock").classList.toggle("collapsed");
  syncDockSpace();
});
document.getElementById("btn-chat-toggle").addEventListener("click", () => {
  document.getElementById("chat-dock").classList.toggle("collapsed");
  syncDockSpace();
});
document.getElementById("chat-dock-move").addEventListener("click", async () => {
  const dock = document.getElementById("chat-dock");
  const next = dock.dataset.position === "bottom" ? "right" : "bottom";
  dock.dataset.position = next;
  syncDockSpace();
  const spec = JSON.parse(JSON.stringify(STATE.layout));
  spec.chat_dock = spec.chat_dock || {}; spec.chat_dock.position = next;
  track("move", "chat-dock", { position: next });
  await post("/api/layout", { user_id: USER, spec });
  STATE.layout.chat_dock = spec.chat_dock;
});

/* ---------------- top bar ---------------- */
document.getElementById("btn-evolve").addEventListener("click", async () => {
  const b = document.getElementById("btn-evolve");
  b.disabled = true; b.innerHTML = `<span class="spin">◐</span> Reviewing`;
  try {
    const r = await post(`/api/curator/run?user_id=${USER}`, {});
    await loadState();
    if (r.proposal) document.getElementById("proposal-tray").classList.add("open");
    else toast("Curator: nothing worth changing yet — keep using the board");
  } finally { b.disabled = false; b.textContent = "⚗ Evolve"; }
});
document.getElementById("btn-proposals").addEventListener("click", () =>
  document.getElementById("proposal-tray").classList.toggle("open"));
document.getElementById("tray-close").addEventListener("click", () =>
  document.getElementById("proposal-tray").classList.remove("open"));

/* ---------------- live updates (SSE) ---------------- */
function connectSSE() {
  const es = new EventSource("/api/events");
  es.onmessage = async (e) => {
    try {
      const ev = JSON.parse(e.data);
      if (PREVIEW) return; // don't yank the user out of preview
      if (ev.kind === "layout_changed") {
        await loadState();
        toast("Dashboard updated by Hermes");
      } else if (ev.kind === "proposal") {
        await loadState();
        addMsg("agent", "⚗ New evolution proposal — check the ▣ tray.");
      } else if (ev.kind === "tool" && ev.scope === "main") {
        addToolMsg(ev.name);
      }
    } catch {}
  };
  es.onerror = () => { es.close(); setTimeout(connectSSE, 4000); };
}
connectSSE();

/* periodic data refresh */
setInterval(() => {
  if (!STATE) return;
  for (const c of activeSpec().components || []) if (!c.hidden) loadComponentData(c);
}, 45000);

loadState().then(() => {
  if (STATE && STATE.onboarded)
    addMsg("agent", "Station online. I'm your full agent here — ask me anything, or tell me to reshape the board (right-click any card to work with just that component).");
});
