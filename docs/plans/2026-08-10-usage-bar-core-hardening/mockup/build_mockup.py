"""Build the M1/M2 high-fidelity mockup HTML.

Inlines the REAL design tokens extracted from apps/desktop/src/styles.css
(`_tokens_light.txt` / `_tokens_dark.txt`, extracted from the live `:root` and
`:root.dark` blocks) so the mockup renders with production light/dark values.

Usage: python build_mockup.py  ->  usage-bar-mockup.html (+ index.html)
Scenes via query params: ?scene=statusbar|popover|command-center|palette
                         &theme=light|dark  &vp=wide|narrow
"""
from __future__ import annotations

import pathlib

HERE = pathlib.Path(__file__).parent

LIGHT = (HERE / "_tokens_light.txt").read_text(encoding="utf-8")
DARK = (HERE / "_tokens_dark.txt").read_text(encoding="utf-8")

TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Usage Bar M1/M2 Mockup</title>
<style>
/* ===== REAL TOKENS from apps/desktop/src/styles.css (2026-08-10) ===== */
__LIGHT_TOKENS__
__DARK_TOKENS__
/* ===== applyTheme() inline seeds — default Nous skin, dark mode =====
   (styles.css :root.dark only holds mix knobs; the real app injects these
   per-skin values inline. Source: apps/desktop/src/themes/presets.ts
   nousTheme.darkColors + themes/context.tsx applyTheme.) */
:root.dark {
  --theme-foreground: #FFE6CB;
  --theme-primary: #FFE6CB;
  --theme-secondary: #1B45A4;
  --theme-accent-soft: #1540B1;
  --theme-midground: #0053FD;
  --theme-warm: #FFE6CB;
  --theme-background-seed: #0D2F86;
  --theme-sidebar-seed: #09286F;
  --theme-card-seed: #12378F;
  --theme-elevated-seed: #123A96;
  --theme-bubble-seed: #143B91;
  --dt-primary-foreground: #0D2F86;
  --dt-muted: #183F9A;
  --dt-border: #3158AD;
  --dt-ring: #FFE6CB;
  --dt-destructive: #C0473A;
}
/* ===== semantic aliases mirroring @theme inline mapping ===== */
:root {
  --color-background: var(--dt-background);
  --color-foreground: var(--dt-foreground);
  --color-card: var(--dt-card);
  --color-muted: var(--dt-muted);
  --color-muted-foreground: var(--dt-muted-foreground);
  --color-primary: var(--dt-primary, var(--theme-primary));
  --color-destructive: var(--dt-destructive, var(--ui-red));
  --color-border: var(--dt-border, var(--ui-stroke-secondary));
}
/* ===== mockup frame ===== */
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  background: var(--color-background);
  color: var(--color-foreground);
  font: 400 0.8125rem/1.45 -apple-system, "Segoe UI", system-ui, sans-serif;
  display: flex; flex-direction: column;
}
.app { flex: 1; display: flex; flex-direction: column; justify-content: flex-end; position: relative; min-height: 100vh; }
.stage { flex: 1; display: flex; align-items: center; justify-content: center; color: var(--color-muted-foreground); font-size: 0.75rem; }
/* ===== statusbar (compact quick layer) ===== */
.statusbar {
  display: flex; align-items: center; gap: 0.75rem;
  height: 1.75rem; padding: 0 0.625rem;
  border-top: 1px solid var(--ui-stroke-tertiary);
  background: var(--color-background);
  font-size: 0.6875rem; color: var(--color-muted-foreground);
  white-space: nowrap; overflow: hidden;
}
.sb-item { display: inline-flex; align-items: center; gap: 0.375rem; }
.sb-btn {
  display: inline-flex; align-items: center; gap: 0.375rem;
  border: 0; background: none; color: inherit; font: inherit;
  padding: 0.125rem 0.375rem; border-radius: 4px; cursor: pointer;
}
.sb-btn:hover { background: var(--chrome-action-hover); }
.sb-btn:focus-visible { outline: 2px solid var(--theme-primary); outline-offset: 1px; }
.sb-spacer { flex: 1; }
.dot { width: 6px; height: 6px; border-radius: 50%; background: var(--ui-green); }
.meter-mini { display: inline-flex; align-items: center; gap: 0.25rem; }
.meter-mini .track { width: 42px; height: 4px; border-radius: 999px; background: var(--color-muted); overflow: hidden; }
.meter-mini .fill { height: 100%; border-radius: 999px; background: var(--color-primary); }
.tabular { font-variant-numeric: tabular-nums; }
/* ===== popover (account limits) — mirrors ContextUsagePanel ===== */
.popover {
  position: absolute; right: 0.5rem; bottom: 2.25rem;
  width: 20rem; max-height: min(34rem, 80vh); overflow-y: auto;
  display: flex; flex-direction: column; gap: 1rem;
  padding: 0.75rem; font-size: 0.75rem;
  background: var(--color-card);
  border: 1px solid var(--ui-stroke-secondary);
  border-radius: 10px;
  box-shadow: var(--shadow-nous);
}
.vp-narrow .popover { width: calc(100vw - 1rem); right: 0.5rem; }
.row { display: flex; align-items: baseline; justify-content: space-between; gap: 0.5rem; }
.row-center { display: flex; align-items: center; justify-content: space-between; gap: 0.5rem; }
.title { font-weight: 500; color: var(--color-foreground); }
.small { font-size: 0.6875rem; }
.muted { color: var(--color-muted-foreground); }
.fg { color: var(--color-foreground); }
.truncate { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; }
.section { display: flex; flex-direction: column; gap: 0.75rem; border-top: 1px solid var(--ui-stroke-tertiary); padding-top: 0.75rem; }
.stack { display: flex; flex-direction: column; gap: 0.375rem; }
.stack-lg { display: flex; flex-direction: column; gap: 0.625rem; }
.progress { position: relative; width: 100%; height: 4px; overflow: hidden; border-radius: 999px; background: var(--color-muted); }
.progress > i { display: block; height: 100%; border-radius: 999px; background: var(--color-primary); }
.progress.warn > i { background: var(--ui-warm); }
.progress.crit > i { background: var(--color-destructive); }
.ctxbar { display: flex; height: 6px; overflow: hidden; border-radius: 999px; background: var(--ui-stroke-tertiary); }
.ctxbar > span { height: 100%; min-width: 1px; }
.badge {
  display: inline-flex; align-items: center; gap: 0.25rem;
  font-size: 0.625rem; line-height: 1; padding: 0.1875rem 0.375rem;
  border-radius: 999px; font-weight: 500;
}
.badge-current { color: var(--color-primary); background: var(--theme-accent-soft); }
.badge-stale { color: var(--ui-warm); background: color-mix(in srgb, var(--ui-warm) 12%, transparent); }
.badge-err { color: var(--color-destructive); background: color-mix(in srgb, var(--color-destructive) 10%, transparent); }
.badge-plan { color: var(--color-muted-foreground); border: 1px solid var(--ui-stroke-tertiary); }
.alert { display: flex; align-items: flex-start; gap: 0.5rem; color: var(--color-destructive); }
.vp-narrow .alert { flex-direction: column; gap: 0.25rem; }
.alert .muted { color: var(--color-muted-foreground); }
.link { color: var(--color-primary); text-decoration: none; cursor: pointer; background: none; border: 0; font: inherit; padding: 0; }
.link:hover { text-decoration: underline; }
.link:focus-visible { outline: 2px solid var(--theme-primary); outline-offset: 1px; border-radius: 2px; }
/* ===== command center overlay (M2) ===== */
.overlay-backdrop { position: absolute; inset: 0; background: color-mix(in srgb, #000 18%, transparent); display: flex; align-items: center; justify-content: center; }
.overlay {
  width: min(44rem, 92vw); max-height: 84vh; overflow-y: auto;
  background: var(--color-card);
  border: 1px solid var(--stroke-nous);
  border-radius: 14px; box-shadow: var(--shadow-nous);
  padding: 1.25rem 1.5rem; display: flex; flex-direction: column; gap: 1.25rem;
}
.cc-head { display: flex; align-items: center; justify-content: space-between; }
.cc-title { font-size: 1rem; font-weight: 600; }
.cc-close { border: 0; background: none; color: var(--color-muted-foreground); cursor: pointer; font-size: 0.875rem; padding: 0.25rem 0.5rem; border-radius: 6px; }
.cc-close:hover { background: var(--chrome-action-hover); }
.cc-close:focus-visible { outline: 2px solid var(--theme-primary); outline-offset: 1px; }
.cc-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem 1.5rem; }
.vp-narrow .cc-grid { grid-template-columns: 1fr; }
.dim-note { font-size: 0.625rem; letter-spacing: 0.02em; text-transform: uppercase; color: var(--color-muted-foreground); }
/* ===== command palette ===== */
.palette {
  width: min(30rem, 92vw); background: var(--color-card);
  border: 1px solid var(--stroke-nous); border-radius: 12px;
  box-shadow: var(--shadow-nous); overflow: hidden;
}
.palette input {
  width: 100%; border: 0; outline: none; background: none; color: var(--color-foreground);
  font: inherit; padding: 0.75rem 1rem; border-bottom: 1px solid var(--ui-stroke-tertiary);
}
.palette-item { display: flex; align-items: center; gap: 0.625rem; padding: 0.5rem 1rem; font-size: 0.75rem; cursor: pointer; width: 100%; background: none; border: 0; color: var(--color-foreground); font-family: inherit; text-align: left; }
.palette-item.active { background: var(--theme-accent-soft); }
.palette-item:hover { background: var(--chrome-action-hover); }
.palette-item:focus-visible { outline: 2px solid var(--theme-primary); outline-offset: -2px; }
.palette-item .kbd { margin-left: auto; font-size: 0.625rem; color: var(--color-muted-foreground); }
/* ===== annotations ===== */
.note { position: absolute; top: 0.75rem; left: 0.75rem; max-width: 22rem; font-size: 0.6875rem; color: var(--color-muted-foreground); background: var(--color-card); border: 1px dashed var(--ui-stroke-secondary); border-radius: 8px; padding: 0.5rem 0.75rem; }
.note b { color: var(--color-foreground); }
@media (prefers-reduced-motion: reduce) {
  * { transition: none !important; animation: none !important; }
}
</style>
</head>
<body>
<script>
const q = new URLSearchParams(location.search);
const scene = q.get('scene') || 'index';
const theme = q.get('theme') || 'light';
const vp = q.get('vp') || 'wide';
if (theme === 'dark') document.documentElement.classList.add('dark');
if (vp === 'narrow') document.body.classList.add('vp-narrow');
if (q.get('full') === '1') {
  // Screenshot aid only: the real component keeps max-h min(34rem,80vh) + scroll.
  const st = document.createElement('style');
  st.textContent = '.popover{max-height:none !important; overflow:visible !important}';
  document.head.appendChild(st);
}
</script>
__SCENES__
<script>
// scene router
const scenes = document.querySelectorAll('[data-scene]');
scenes.forEach(s => { s.style.display = (s.dataset.scene === scene) ? '' : 'none'; });
if (scene === 'index') document.querySelector('[data-scene="index"]').style.display = '';

// Minimal real keyboard path (mockup scope): the statusbar trigger toggles the
// popover; Escape closes it and returns focus to the trigger. Focus trap /
// roving tabindex remain production acceptance targets (see index notes).
if (scene === 'popover') {
  const root = document.querySelector('[data-scene="popover"]');
  const trigger = root.querySelector('[data-popover-trigger]');
  const pop = root.querySelector('.popover');
  const setOpen = (open) => {
    pop.style.display = open ? '' : 'none';
    trigger.setAttribute('aria-expanded', String(open));
    if (!open) trigger.focus();
  };
  trigger.addEventListener('click', () => setOpen(pop.style.display === 'none'));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && pop.style.display !== 'none') setOpen(false);
  });
}
</script>
</body>
</html>
"""

SCENES = r"""
<!-- ================= INDEX / 说明 ================= -->
<div class="app" data-scene="index" style="justify-content:flex-start; padding:2rem; gap:1rem; overflow-y:auto">
  <h1 style="font-size:1.125rem">Usage Bar 内建化 — M1/M2 高保真 Mockup</h1>
  <p class="muted small">token 直接内联自 <code>apps/desktop/src/styles.css</code> 的 <code>:root</code> / <code>:root.dark</code>（2026-08-10 提取），light/dark 为生产真实值。所有账号名为稳定哈希派生展示名，不含任何凭证/邮箱/内部 label。</p>
  <ul class="small" style="display:flex; flex-direction:column; gap:0.375rem; list-style:none">
    <li>① 状态栏快捷摘要：<a class="link" href="?scene=statusbar&amp;theme=light">light</a> · <a class="link" href="?scene=statusbar&amp;theme=dark">dark</a></li>
    <li>② Account Limits popover：<a class="link" href="?scene=popover&amp;theme=light">light</a> · <a class="link" href="?scene=popover&amp;theme=dark">dark</a> · <a class="link" href="?scene=popover&amp;theme=light&amp;vp=narrow">窄视口 360px</a></li>
    <li>③ Command Center → Usage（M2）：<a class="link" href="?scene=command-center&amp;theme=light">light</a> · <a class="link" href="?scene=command-center&amp;theme=dark">dark</a> · <a class="link" href="?scene=command-center&amp;theme=light&amp;vp=narrow">窄视口</a></li>
    <li>④ 状态栏整体关闭时的到达路径（Command Palette）：<a class="link" href="?scene=palette&amp;theme=light">light</a> · <a class="link" href="?scene=palette&amp;theme=dark">dark</a></li>
  </ul>
  <div class="stack small" style="border-top:1px solid var(--ui-stroke-tertiary); padding-top:0.75rem; max-width:40rem">
    <p><b>进度条语义</b>：Account limits 的 bar fill = remaining，与 <code>xx% left</code> 文字同值（父验收 B1 统一）；By provider/model/task 的 bar = 占比份额，aria-label 各自说明。</p>
    <p><b>键盘（mockup 内真实实现的最小路径）</b>：状态栏触发器为真实 <code>&lt;button&gt;</code>，Tab 可达、<code>:focus-visible</code> 主题色环；popover 场景的 Esc 真实关闭面板并返还焦点给触发器（可现场验证）；面板内 "Open in Command Center" 为真实 <code>&lt;a href&gt;</code>（跳转本 mockup 的 CC 场景）、CC 的 "Refresh" 为真实 <code>&lt;button&gt;</code>；palette rows 为真实 <code>&lt;button role="option"&gt;</code>。</p>
    <p><b>Production implementation acceptance targets（mockup 未实现，不声称已实现）</b>：focus trap、roving tabindex（方向键在窗口行间移动）、打开后焦点自动进入面板、live region 对 stale/error 状态变化的动态播报。这些在 M1/M2 生产实现时验收。</p>
    <p><b>reduced-motion</b>：全部过渡包在 <code>@media (prefers-reduced-motion: reduce)</code> 下禁用（本页底部媒体查询演示）；reduced 下进度条宽度即时生效、popover 无位移。</p>
    <p><b>live region</b>：stale 状态行带稳定 <code>role="status"</code>（polite）；provider error 用 <code>role="alert"</code>。</p>
    <p><b>真相来源分离</b>：provider quota（官方接口，用户文案 "Official provider data"）/ credential health（本地凭证池状态）/ local analytics（本地计量）三区独立渲染、独立 loading/error，不混算、不跨窗口平均。</p>
    <p><b>stale 语义</b>：仅可重试故障（timeout/network/remote-protocol/proxy/5xx）回退 stale 并标注 "Cached · 时间"；401/403/429 与本地配置错误（UnsupportedProtocol/LocalProtocolError）直接表面化，绝不用 stale 掩盖。</p>
  </div>
</div>

<!-- ================= ① 状态栏快捷摘要 ================= -->
<div class="app" data-scene="statusbar">
  <div class="note"><b>① 状态栏快捷摘要</b> — 单行紧凑层。context-usage 默认可见（M0 已固化）；点击打开 Account Limits popover（场景②）。完整分析入口在 Command Center（场景③）。</div>
  <div class="stage">— transcript / composer 区域 —</div>
  <div class="statusbar">
    <span class="sb-item"><span class="dot"></span>Gateway</span>
    <span class="sb-item truncate">aichat_group · main</span>
    <span class="sb-spacer"></span>
    <button class="sb-btn" aria-label="Context and account usage">
      <span class="meter-mini"><span class="track"><span class="fill" style="width:38%"></span></span></span>
      <span class="tabular">Ctx 38%</span>
      <span class="muted">·</span>
      <span class="tabular fg">Codex 41% wk</span>
    </button>
    <span class="sb-item muted">k3 · kimi-coding</span>
  </div>
</div>

<!-- ================= ② Account Limits popover ================= -->
<div class="app" data-scene="popover">
  <div class="note"><b>② Account Limits popover</b> — 触发器带焦点环（演示）。覆盖：稳定账号名 Codex 1/2、当前 badge、plan、reset countdown、window detail、source、freshness、stale、provider-specific error（401 不用 stale 掩盖）。</div>
  <div class="stage">— transcript / composer 区域 —</div>
  <div class="popover" role="dialog" aria-label="Context and account usage">
    <div class="row"><p class="title">Context usage</p><span class="small muted">~76k / 200k tokens</span></div>
    <p class="small fg tabular">38% full</p>
    <div class="ctxbar"><span style="background:var(--theme-primary);width:22%"></span><span style="background:var(--ui-warm);width:9%"></span><span style="background:var(--ui-green);width:7%"></span></div>
    <div class="section" aria-label="Account limits">
      <div class="row"><p class="title">Account limits</p><span class="small muted">Official provider data</span></div>

      <!-- openai-codex -->
      <div class="stack-lg">
        <div class="row"><span class="title truncate">openai-codex</span><span class="small muted">2/2 ready</span></div>
        <div class="stack">
          <div class="row">
            <span class="fg truncate">Codex 1 <span class="badge badge-current">current</span> <span class="badge badge-plan">Plus</span></span>
            <span class="small muted">ready</span>
          </div>
          <div class="stack">
            <div class="row-center small"><span class="muted truncate">5 hour</span><span class="fg tabular">68% left · resets in 2h 14m</span></div>
            <div class="progress" role="progressbar" aria-valuenow="68" aria-valuemin="0" aria-valuemax="100" aria-label="5 hour: 68% left"><i style="width:68%"></i></div>
            <div class="row-center small"><span class="muted truncate">Weekly</span><span class="fg tabular">41% left · resets in 3d 4h</span></div>
            <div class="progress warn" role="progressbar" aria-valuenow="41" aria-valuemin="0" aria-valuemax="100" aria-label="Weekly: 41% left"><i style="width:41%"></i></div>
            <p class="small muted">updated 12s ago</p>
          </div>
        </div>
        <div class="stack">
          <div class="row">
            <span class="fg truncate">Codex 2 <span class="badge badge-plan">Plus</span> <span class="badge badge-stale">stale</span></span>
            <span class="small muted">ready</span>
          </div>
          <div class="stack">
            <div class="row-center small"><span class="muted truncate">Weekly</span><span class="fg tabular">77% left · resets in 5d 1h</span></div>
            <div class="progress" role="progressbar" aria-valuenow="77" aria-valuemin="0" aria-valuemax="100" aria-label="Weekly: 77% left"><i style="width:77%"></i></div>
            <p class="small muted" role="status">Cached · 11:42:03 — request timed out, showing last good read</p>
          </div>
        </div>
      </div>

      <!-- kimi-coding -->
      <div class="stack-lg">
        <div class="row"><span class="title truncate">kimi-coding</span><span class="small muted">1/1 ready</span></div>
        <div class="stack">
          <div class="row">
            <span class="fg truncate">Kimi 1 <span class="badge badge-plan">Level 2</span></span>
            <span class="small muted">ready</span>
          </div>
          <div class="stack">
            <div class="row-center small"><span class="muted truncate">Weekly</span><span class="fg tabular">73% left · resets in 4d 6h</span></div>
            <div class="progress" role="progressbar" aria-valuenow="73" aria-valuemin="0" aria-valuemax="100" aria-label="Weekly: 73% left"><i style="width:73%"></i></div>
            <div class="row-center small"><span class="muted truncate">5 hour</span><span class="fg tabular">90% left · resets in 47m</span></div>
            <div class="progress" role="progressbar" aria-valuenow="90" aria-valuemin="0" aria-valuemax="100" aria-label="5 hour: 90% left"><i style="width:90%"></i></div>
            <p class="small muted">Parallel limit: 5 · updated 12s ago</p>
          </div>
        </div>
      </div>

      <!-- anthropic: provider-specific error（认证失效，非 stale） -->
      <div class="stack-lg">
        <div class="row"><span class="title truncate">anthropic</span><span class="small muted">0/1 ready</span></div>
        <div class="stack">
          <div class="row"><span class="fg truncate">Claude 1</span><span class="small muted">cooldown</span></div>
          <div class="alert small" role="alert">
            <span>⚠ Credential token expired — re-authenticate.</span>
            <span class="muted">401 responses are never masked by stale cache.</span>
          </div>
        </div>
      </div>

      <div class="row"><span class="small muted">Local analytics · this session</span><a class="link small" href="?scene=command-center">Open in Command Center</a></div>
    </div>
  </div>
  <div class="statusbar">
    <span class="sb-item"><span class="dot"></span>Gateway</span>
    <span class="sb-item truncate">aichat_group · main</span>
    <span class="sb-spacer"></span>
    <button class="sb-btn" data-popover-trigger style="outline:2px solid var(--theme-primary); outline-offset:1px" aria-expanded="true" aria-haspopup="dialog" aria-label="Context and account usage">
      <span class="meter-mini"><span class="track"><span class="fill" style="width:38%"></span></span></span>
      <span class="tabular">Ctx 38%</span><span class="muted">·</span><span class="tabular fg">Codex 41% wk</span>
    </button>
    <span class="sb-item muted">k3 · kimi-coding</span>
  </div>
</div>

<!-- ================= ③ Command Center → Usage (M2) ================= -->
<div class="app" data-scene="command-center">
  <div class="overlay-backdrop">
    <div class="overlay" role="dialog" aria-label="Command Center — Usage">
      <div class="cc-head"><span class="cc-title">Command Center · Usage</span><button class="cc-close" aria-label="Close">✕</button></div>
      <div class="stack-lg">
        <div class="row"><p class="title">Account limits</p><span class="small muted">Official provider data · refreshed 12s ago · <button class="link small" type="button">Refresh</button></span></div>
        <div class="cc-grid">
          <div class="stack">
            <div class="row"><span class="fg">Codex 1 <span class="badge badge-current">current</span> <span class="badge badge-plan">Plus</span></span><span class="small muted">openai-codex</span></div>
            <div class="row-center small"><span class="muted">5 hour</span><span class="fg tabular">68% left · resets in 2h 14m</span></div>
            <div class="progress" role="progressbar" aria-valuenow="68" aria-valuemin="0" aria-valuemax="100" aria-label="5 hour: 68% left"><i style="width:68%"></i></div>
            <div class="row-center small"><span class="muted">Weekly</span><span class="fg tabular">41% left · resets in 3d 4h</span></div>
            <div class="progress warn" role="progressbar" aria-valuenow="41" aria-valuemin="0" aria-valuemax="100" aria-label="Weekly: 41% left"><i style="width:41%"></i></div>
          </div>
          <div class="stack">
            <div class="row"><span class="fg">Codex 2 <span class="badge badge-plan">Plus</span> <span class="badge badge-stale">stale</span></span><span class="small muted">openai-codex</span></div>
            <div class="row-center small"><span class="muted">Weekly</span><span class="fg tabular">77% left · resets in 5d 1h</span></div>
            <div class="progress" role="progressbar" aria-valuenow="77" aria-valuemin="0" aria-valuemax="100" aria-label="Weekly: 77% left"><i style="width:77%"></i></div>
            <p class="small muted" role="status">Cached · 11:42:03 — transient upstream timeout</p>
          </div>
          <div class="stack">
            <div class="row"><span class="fg">Kimi 1 <span class="badge badge-plan">Level 2</span></span><span class="small muted">kimi-coding</span></div>
            <div class="row-center small"><span class="muted">Weekly</span><span class="fg tabular">73% left · resets in 4d 6h</span></div>
            <div class="progress" role="progressbar" aria-valuenow="73" aria-valuemin="0" aria-valuemax="100" aria-label="Weekly: 73% left"><i style="width:73%"></i></div>
            <p class="small muted">Parallel limit: 5</p>
          </div>
          <div class="stack">
            <div class="row"><span class="fg">Claude 1</span><span class="small muted">anthropic</span></div>
            <div class="alert small" role="alert"><span>⚠ Credential token expired — re-authenticate</span></div>
          </div>
        </div>
      </div>
      <div class="section">
        <div class="row"><p class="title">Provider / Model / Task</p><span class="dim-note">Local analytics — 与 provider quota 分离，不混算</span></div>
        <div class="cc-grid">
          <div class="stack">
            <span class="dim-note">By provider (7d, local)</span>
            <div class="row-center small"><span class="muted">kimi-coding</span><span class="fg tabular">1.2M tok · 214 calls</span></div>
            <div class="progress" role="progressbar" aria-valuenow="64" aria-valuemin="0" aria-valuemax="100" aria-label="kimi-coding: 64% of local tokens"><i style="width:64%"></i></div>
            <div class="row-center small"><span class="muted">openai-codex</span><span class="fg tabular">0.6M tok · 88 calls</span></div>
            <div class="progress" role="progressbar" aria-valuenow="32" aria-valuemin="0" aria-valuemax="100" aria-label="openai-codex: 32% of local tokens"><i style="width:32%"></i></div>
          </div>
          <div class="stack">
            <span class="dim-note">By model (7d, local)</span>
            <div class="row-center small"><span class="muted">k3</span><span class="fg tabular">0.9M tok</span></div>
            <div class="progress" role="progressbar" aria-valuenow="48" aria-valuemin="0" aria-valuemax="100" aria-label="k3: 48% of local tokens"><i style="width:48%"></i></div>
            <div class="row-center small"><span class="muted">gpt-5.3-codex</span><span class="fg tabular">0.6M tok</span></div>
            <div class="progress" role="progressbar" aria-valuenow="32" aria-valuemin="0" aria-valuemax="100" aria-label="gpt-5.3-codex: 32% of local tokens"><i style="width:32%"></i></div>
          </div>
          <div class="stack">
            <span class="dim-note">By task (7d, local)</span>
            <div class="row-center small"><span class="muted">coding</span><span class="fg tabular">1.1M tok · 61%</span></div>
            <div class="progress" role="progressbar" aria-valuenow="61" aria-valuemin="0" aria-valuemax="100" aria-label="coding: 61% of local tokens"><i style="width:61%"></i></div>
            <div class="row-center small"><span class="muted">tool ops</span><span class="fg tabular">0.4M tok · 22%</span></div>
            <div class="progress" role="progressbar" aria-valuenow="22" aria-valuemin="0" aria-valuemax="100" aria-label="tool ops: 22% of local tokens"><i style="width:22%"></i></div>
            <div class="row-center small"><span class="muted">research</span><span class="fg tabular">0.3M tok · 17%</span></div>
            <div class="progress" role="progressbar" aria-valuenow="17" aria-valuemin="0" aria-valuemax="100" aria-label="research: 17% of local tokens"><i style="width:17%"></i></div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<!-- ================= ④ Command Palette 到达路径 ================= -->
<div class="app" data-scene="palette">
  <div class="note"><b>④ 状态栏整体关闭时</b>：Command Palette 输入 "usage" 直达 Command Center → Usage；同一 action，同一状态（one action, one home）。</div>
  <div class="stage" style="align-items:flex-start; padding-top:12vh">
    <div class="palette" role="dialog" aria-label="Command palette">
      <input value="usage" aria-label="Command search" readonly>
      <div role="listbox" aria-label="Commands">
        <button class="palette-item active" type="button" role="option" aria-selected="true">▸ Open Usage — Command Center<span class="kbd">Enter</span></button>
        <button class="palette-item" type="button" role="option" aria-selected="false">▸ Refresh account limits<span class="kbd">usage.accounts</span></button>
        <button class="palette-item" type="button" role="option" aria-selected="false">▸ Toggle statusbar visibility<span class="kbd">Settings</span></button>
      </div>
    </div>
  </div>
  <div class="statusbar" style="opacity:0.35"><span class="sb-item muted">（statusbar 已整体关闭 — 仅为演示占位）</span></div>
</div>
"""

html = TEMPLATE.replace("__LIGHT_TOKENS__", LIGHT).replace("__DARK_TOKENS__", DARK).replace("__SCENES__", SCENES)
out = HERE / "usage-bar-mockup.html"
# newline="" keeps LF on disk (Windows write_text would otherwise emit CRLF,
# which fails the repo's `git diff --check` trailing-whitespace gate).
out.write_text(html, encoding="utf-8", newline="")
print(f"wrote {out} ({len(html)} bytes)")
