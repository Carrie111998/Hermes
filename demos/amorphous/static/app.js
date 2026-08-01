/* Amorphous Applications — frontend
 * Vanilla JS: grid renderer, telemetry collector (clicks / dwell / hide / move),
 * invariant chat dock, proposal tray, workflow runners.
 */
const USER = new URLSearchParams(location.search).get("user") || "demo";
let STATE = null;
const telemetryQueue = [];

/* ---------------- telemetry ---------------- */
function track(type, componentId, payload) {
  telemetryQueue.push({ type, component_id: componentId || null, payload: payload || null });
}
setInterval(async () => {
  if (!telemetryQueue.length) return;
  const events = telemetryQueue.splice(0, telemetryQueue.length);
  try {
    await fetch("/api/telemetry", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: USER, events }),
    });
  } catch (e) { /* re-queue on failure */ telemetryQueue.unshift(...events); }
}, 4000);

/* dwell tracking: mouseenter/leave per card */
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

/* ---------------- api ---------------- */
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(await r.text());
  return r.json();
}
async function loadState() {
  STATE = await api(`/api/state?user_id=${USER}`);
  renderAll();
}

/* ---------------- grid render ---------------- */
let PREVIEW = null; // {proposal, spec, diff} while trying a proposed dashboard

function renderAll() {
  const badge = document.getElementById("agent-badge");
  badge.textContent = `agent: ${STATE.agent.model}`;
  badge.classList.toggle("live", STATE.agent.live);
  renderGrid();
  renderProposals();
  renderChatDockPosition();
  renderPreviewBanner();
}

function activeSpec() {
  return PREVIEW ? PREVIEW.spec : STATE.layout;
}

function renderPreviewBanner() {
  let bar = document.getElementById("preview-banner");
  if (!PREVIEW) { if (bar) bar.remove(); return; }
  if (!bar) {
    bar = document.createElement("div");
    bar.id = "preview-banner";
    document.body.insertBefore(bar, document.getElementById("grid"));
  }
  const diffTxt = PREVIEW.diff.map((d) => `${d.change}: ${d.title}`).join(" · ") || "layout reflow only";
  bar.innerHTML = `<span>👁 <b>Previewing proposed dashboard</b> — ${esc(diffTxt)}</span>
    <span class="pv-actions">
      <button class="approve" id="pv-approve">✔ Keep it</button>
      <button class="reject" id="pv-reject">✕ Go back</button>
    </span>`;
  document.getElementById("pv-approve").addEventListener("click", async () => {
    await actProposal(PREVIEW.proposal.id, "approve", "", "up");
    PREVIEW = null;
    await loadState();
  });
  document.getElementById("pv-reject").addEventListener("click", async () => {
    const why = prompt("Optional: tell the curator why you're keeping the current dashboard\n" +
      "(this steers the next evolution away from these changes)") || "";
    await actProposal(PREVIEW.proposal.id, "reject", why, "down");
    PREVIEW = null;
    await loadState();
  });
}

function renderGrid() {
  const grid = document.getElementById("grid");
  grid.innerHTML = "";
  const spec = activeSpec();
  const comps = spec.components || [];
  for (const c of comps) {
    if (c.hidden) continue;
    const card = document.createElement("div");
    card.className = "card";
    card.style.gridColumn = `${(c.col || 0) + 1} / span ${Math.min(c.w || 3, 12)}`;
    card.dataset.cid = c.id;
    card.innerHTML = `
      <div class="card-head">
        <span class="title">${esc(c.title)}</span>
        <span class="card-tools">
          <button class="ghost t-up" title="Move up">↑</button>
          <button class="ghost t-hide" title="Hide">—</button>
        </span>
      </div>
      <div class="card-body" id="body-${c.id}">…</div>`;
    grid.appendChild(card);
    attachDwell(card, c.id);
    card.addEventListener("click", () => track("click", c.id));
    if (PREVIEW) {
      card.querySelector(".card-tools").style.display = "none";
    } else {
      card.querySelector(".t-hide").addEventListener("click", (e) => {
        e.stopPropagation(); hideComponent(c.id);
      });
      card.querySelector(".t-up").addEventListener("click", (e) => {
        e.stopPropagation(); moveComponentUp(c.id);
      });
    }
    loadComponentData(c);
    track("view", c.id);
  }
  const hidden = comps.filter((c) => c.hidden);
  if (hidden.length && !PREVIEW) {
    const bar = document.createElement("div");
    bar.className = "hidden-comps";
    bar.append(`Hidden: `);
    for (const c of hidden) {
      const b = document.createElement("button");
      b.textContent = `+ ${c.title}`;
      b.addEventListener("click", () => showComponent(c.id));
      bar.appendChild(b);
    }
    grid.appendChild(bar);
  }
}

async function loadComponentData(c) {
  const body = document.getElementById(`body-${c.id}`);
  if (!body) return;
  try {
    const pv = PREVIEW ? `&proposal_id=${PREVIEW.proposal.id}` : "";
    const d = await api(`/api/component/${c.id}/data?user_id=${USER}${pv}`);
    body.innerHTML = "";
    body.appendChild(renderData(c, d));
  } catch (e) {
    body.textContent = `⚠ ${e.message}`.slice(0, 200);
  }
}

function renderData(c, d) {
  const el = document.createElement("div");
  switch (d.kind) {
    case "metric": {
      const dir = d.delta > 0 ? "up" : "down";
      el.innerHTML = `<div class="metric-value">${d.value}<span class="metric-unit">${esc(d.unit || "")}</span></div>
        <div class="metric-delta ${dir}">${d.delta > 0 ? "▲" : "▼"} ${Math.abs(d.delta)} vs prev</div>`;
      break;
    }
    case "timeseries": el.appendChild(sparkline(d.points)); break;
    case "table": {
      const t = document.createElement("table");
      t.innerHTML = `<thead><tr>${d.columns.map((x) => `<th>${esc(x)}</th>`).join("")}</tr></thead>
        <tbody>${d.rows.map((r) => `<tr>${r.map((v) => `<td>${esc(String(v))}</td>`).join("")}</tr>`).join("")}</tbody>`;
      el.appendChild(t);
      break;
    }
    case "links": {
      el.className = "links";
      el.innerHTML = d.links.map((l) => `<a href="${esc(l.url)}" target="_blank">↗ ${esc(l.title)}</a>`).join("");
      break;
    }
    case "statuses": {
      el.innerHTML = d.statuses.map((s) =>
        `<div class="ds-row"><span>${esc(s.name)}</span><span class="ds-mode ${s.mode}">${s.mode}</span></div>`).join("");
      break;
    }
    case "notes": {
      el.className = "notes-md";
      el.textContent = d.markdown;
      break;
    }
    case "workflow": return renderWorkflow(c, d);
    case "activity": {
      el.innerHTML = d.items.length
        ? d.items.map((i) => `<div class="activity-item"><span class="when">${when(i.ts)}</span>${esc(i.text)}</div>`).join("")
        : `<span style="color:var(--dim)">No agent activity yet — run a workflow or chat below.</span>`;
      break;
    }
    case "evolution": {
      el.innerHTML = d.items.length
        ? d.items.map((i) => `<div class="evo-item"><span class="status ${i.status}">${i.status}</span>
            <span class="when">${when(i.when)}</span> ${esc(i.summary)} <em style="color:var(--dim)">(${i.engine})</em></div>`).join("")
        : `<span style="color:var(--dim)">No evolution history yet. Press ⚗ Evolve now after using the dashboard.</span>`;
      break;
    }
    default: el.textContent = d.error || "no data";
  }
  return el;
}

function renderWorkflow(c, d) {
  const el = document.createElement("div");
  if (!d.workflow) { el.textContent = "workflow missing"; return el; }
  const inputs = (c.props.inputs || []);
  el.innerHTML = `<div style="color:var(--dim);font-size:.82rem">${esc(d.workflow.description || "")}</div>
    <div class="wf-inputs">${inputs.map((i) => `<input placeholder="${esc(i.label)}" data-name="${esc(i.name)}">`).join("")}</div>
    <button class="wf-run">▶ Run with Hermes</button>
    <div class="wf-result" style="display:none"></div>`;
  const btn = el.querySelector(".wf-run");
  const resBox = el.querySelector(".wf-result");
  if (d.runs && d.runs.length) {
    resBox.style.display = "block";
    resBox.textContent = `Last run (${when(d.runs[0].ts)}):\n${d.runs[0].result || ""}`;
  }
  btn.addEventListener("click", async (e) => {
    e.stopPropagation();
    const inp = {};
    el.querySelectorAll(".wf-inputs input").forEach((i) => (inp[i.dataset.name] = i.value));
    btn.disabled = true; btn.textContent = "⚙ Running…";
    resBox.style.display = "block"; resBox.textContent = "Hermes is working…";
    try {
      const run = await api(`/api/workflow/${d.workflow.id}/run`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ user_id: USER, inputs: inp }),
      });
      resBox.textContent = run.result;
    } catch (err) { resBox.textContent = `⚠ ${err.message}`; }
    btn.disabled = false; btn.textContent = "▶ Run with Hermes";
  });
  return el;
}

function sparkline(points) {
  const w = 600, h = 120, pad = 4;
  const vals = points.map((p) => p[1]);
  const mn = Math.min(...vals), mx = Math.max(...vals);
  const xs = points.map((_, i) => pad + (i / (points.length - 1)) * (w - 2 * pad));
  const ys = vals.map((v) => h - pad - ((v - mn) / (mx - mn || 1)) * (h - 2 * pad));
  const dAttr = xs.map((x, i) => `${i ? "L" : "M"}${x.toFixed(1)},${ys[i].toFixed(1)}`).join(" ");
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${w} ${h}`);
  svg.classList.add("spark");
  svg.innerHTML = `<path d="${dAttr}" fill="none" stroke="#3fb6a8" stroke-width="2"/>
    <path d="${dAttr} L${xs[xs.length - 1]},${h} L${xs[0]},${h} Z" fill="rgba(63,182,168,.12)" stroke="none"/>`;
  return svg;
}

/* ---------------- layout edits (user) ---------------- */
async function persistLayout() {
  await api("/api/layout", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ user_id: USER, spec: STATE.layout }),
  });
}
function findComp(cid) { return STATE.layout.components.find((c) => c.id === cid); }
async function hideComponent(cid) {
  findComp(cid).hidden = true;
  track("hide", cid);
  await persistLayout(); await loadState();
}
async function showComponent(cid) {
  findComp(cid).hidden = false;
  track("click", cid, { action: "show" });
  await persistLayout(); await loadState();
}
async function moveComponentUp(cid) {
  const comps = STATE.layout.components;
  const i = comps.findIndex((c) => c.id === cid);
  if (i > 0) {
    comps.splice(i - 1, 0, comps.splice(i, 1)[0]);
    track("move", cid);
    await persistLayout(); await loadState();
  }
}

/* ---------------- proposals ---------------- */
function renderProposals() {
  const list = document.getElementById("proposal-list");
  const count = document.getElementById("proposal-count");
  const pending = STATE.proposals || [];
  count.textContent = pending.length;
  list.innerHTML = pending.length ? "" : `<p style="color:var(--dim)">No pending proposals.
    Interact with the dashboard, then press <b>⚗ Evolve now</b> (or wait for the scheduled curator).</p>`;
  for (const p of pending) {
    const div = document.createElement("div");
    div.className = "proposal";
    div.innerHTML = `<div class="engine">${esc(p.engine)} · ${when(p.created_at)}</div>
      <strong>${esc(p.summary)}</strong>
      <ul>${p.mutations.slice(0, 8).map((m) => `<li>${esc(mutLabel(m))}</li>`).join("")}</ul>
      <div style="color:var(--dim);font-size:.8rem">${esc(p.rationale || "")}</div>
      <textarea placeholder="Optional feedback for the curator…"></textarea>
      <div class="actions">
        <button class="try">👁 Try it</button>
        <button class="approve">✔ Approve & apply</button>
        <button class="reject">✕ Reject</button>
      </div>`;
    const fb = () => div.querySelector("textarea").value;
    div.querySelector(".try").addEventListener("click", async () => {
      const pv = await api(`/api/proposal/${p.id}/preview`);
      PREVIEW = { proposal: p, spec: pv.preview, diff: pv.diff };
      document.getElementById("proposal-tray").classList.add("hidden");
      renderAll();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
    div.querySelector(".approve").addEventListener("click", () => actProposal(p.id, "approve", fb(), "up"));
    div.querySelector(".reject").addEventListener("click", () => actProposal(p.id, "reject", fb(), "down"));
    list.appendChild(div);
  }
}
function mutLabel(m) {
  const ops = {
    promote: `Promote ${m.component_id} to the top`,
    shrink: `Shrink ${m.component_id}`,
    hide: `Hide ${m.component_id}`,
    show: `Show ${m.component_id}`,
    remove: `Remove ${m.component_id}`,
    retitle: `Rename ${m.component_id} → "${m.title}"`,
    add: `Add new ${m.component ? m.component.type : "component"}: "${m.component ? m.component.title : ""}"`,
    set_props: `Reconfigure ${m.component_id}`,
    set_notes: `Refresh briefing note`,
    replace_spec: `Full dashboard rebuild (${m.spec && m.spec.components ? m.spec.components.length : 0} components)`,
    move_chat_dock: `Move chat dock to ${m.position}`,
  };
  return ops[m.op] || m.op;
}
async function actProposal(pid, action, feedback, sentiment) {
  await api(`/api/proposal/${pid}`, {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, feedback, sentiment }),
  });
  await loadState();
  if (STATE.proposals.length === 0) document.getElementById("proposal-tray").classList.add("hidden");
}

/* ---------------- chat dock ---------------- */
function renderChatDockPosition() {
  const dock = document.getElementById("chat-dock");
  const pos = (STATE.layout.chat_dock || {}).position || "bottom";
  dock.dataset.position = pos;
}
function addMsg(who, text) {
  const log = document.getElementById("chat-log");
  const div = document.createElement("div");
  div.className = `msg ${who}`;
  div.innerHTML = `<span class="who">${who === "user" ? "you" : "hermes"}</span>${esc(text)}`;
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
  addMsg("agent", "…");
  const log = document.getElementById("chat-log");
  try {
    const r = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_id: USER, text }),
    });
    log.lastChild.remove();
    addMsg("agent", r.reply);
    if (r.proposal) { await loadState(); document.getElementById("proposal-tray").classList.remove("hidden"); }
  } catch (err) {
    log.lastChild.remove();
    addMsg("agent", `⚠ ${err.message}`);
  }
});
document.getElementById("chat-dock-hide").addEventListener("click", () => {
  document.getElementById("chat-dock").classList.toggle("collapsed");
});
document.getElementById("chat-dock-move").addEventListener("click", async () => {
  const dock = document.getElementById("chat-dock");
  const next = dock.dataset.position === "bottom" ? "right" : "bottom";
  dock.dataset.position = next;
  STATE.layout.chat_dock = STATE.layout.chat_dock || {};
  STATE.layout.chat_dock.position = next;
  track("move", "chat-dock", { position: next });
  await persistLayout();
});
document.getElementById("btn-chat-toggle").addEventListener("click", () => {
  document.getElementById("chat-dock").classList.toggle("collapsed");
});

/* ---------------- top bar ---------------- */
document.getElementById("btn-evolve").addEventListener("click", async () => {
  const b = document.getElementById("btn-evolve");
  b.disabled = true; b.textContent = "⚗ Reviewing…";
  try {
    const r = await api(`/api/curator/run?user_id=${USER}`, { method: "POST" });
    await loadState();
    if (r.proposal) document.getElementById("proposal-tray").classList.remove("hidden");
    else alert("Curator found nothing to change yet — interact with the dashboard more (click, run workflows, chat), then retry.");
  } finally { b.disabled = false; b.textContent = "⚗ Evolve now"; }
});
document.getElementById("btn-proposals").addEventListener("click", () => {
  document.getElementById("proposal-tray").classList.toggle("hidden");
});
document.getElementById("tray-close").addEventListener("click", () => {
  document.getElementById("proposal-tray").classList.add("hidden");
});

/* ---------------- utils ---------------- */
function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function when(ts) {
  const d = new Date(ts * 1000);
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/* refresh data periodically */
setInterval(() => {
  if (!STATE) return;
  for (const c of activeSpec().components || []) if (!c.hidden) loadComponentData(c);
}, 30000);

/* live state polling: pick up curator runs / other-tab changes without reload */
let LAST_SIG = "";
setInterval(async () => {
  if (!STATE || PREVIEW) return; // don't yank the user out of a preview
  try {
    const s = await api(`/api/state?user_id=${USER}`);
    const sig = `${s.layout._meta.version}:${s.proposals.length}`;
    if (LAST_SIG && sig !== LAST_SIG) {
      const hadProposals = STATE.proposals.length;
      STATE = s;
      renderAll();
      if (s.proposals.length > hadProposals) {
        addMsg("agent", "⚗ The curator just proposed a dashboard evolution — " +
          "open the ▣ Proposals tray to preview it.");
      }
    }
    LAST_SIG = sig;
  } catch (e) { /* server briefly away; retry next tick */ }
}, 15000);

loadState().then(() => addMsg("agent",
  "Welcome to your Amorphous mission control. I watch how you use this dashboard and evolve it for you. " +
  "Ask me anything, or try /rebuild focus on incident response — proposals land in the ▣ tray for your approval."));
