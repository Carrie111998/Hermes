/* Hash router — routes like '/app/leads/:leadId', guards, mount/cleanup lifecycle.
   Page contract: mount(pageRoot, ctx) -> optional cleanup fn (or a Promise of one).
   ctx = { params, query, navigate, route } */

let _routes = [];
let _beforeEach = null;
let _notFound = null;
let _onError = null;
let _cleanup = null;
let _mountSeq = 0;

function parseHash() {
  const raw = location.hash.replace(/^#/, '') || '/';
  const [pathPart, queryPart] = raw.split('?');
  const query = {};
  if (queryPart) {
    for (const pair of queryPart.split('&')) {
      if (!pair) continue;
      const [k, v] = pair.split('=');
      query[decodeURIComponent(k)] = decodeURIComponent(v || '');
    }
  }
  return { path: pathPart.replace(/\/+$/, '') || '/', query };
}

function matchRoute(path) {
  const segs = path.split('/').filter(Boolean);
  for (const route of _routes) {
    const rsegs = route.path.split('/').filter(Boolean);
    if (rsegs.length !== segs.length) continue;
    const params = {};
    let ok = true;
    for (let i = 0; i < rsegs.length; i++) {
      if (rsegs[i].startsWith(':')) params[rsegs[i].slice(1)] = decodeURIComponent(segs[i]);
      else if (rsegs[i] !== segs[i]) { ok = false; break; }
    }
    if (ok) return { route, params };
  }
  return null;
}

export function navigate(path, { replace = false } = {}) {
  const hash = '#' + path;
  if (replace) {
    const url = location.pathname + location.search + hash;
    history.replaceState(null, '', url);
    handleChange();
  } else if (location.hash === hash) {
    handleChange(); // re-mount same route (e.g. filter reset)
  } else {
    location.hash = hash;
  }
}

export function currentRoute() {
  const { path, query } = parseHash();
  const m = matchRoute(path);
  return { path, query, params: m ? m.params : {} };
}

async function handleChange() {
  const seq = ++_mountSeq;
  const { path, query } = parseHash();
  const m = matchRoute(path);

  if (_beforeEach) {
    const redirect = _beforeEach(m ? { ...m.route, params: m.params, query, path } : { path, query });
    if (redirect && redirect !== path) { navigate(redirect, { replace: true }); return; }
  }

  if (typeof _cleanup === 'function') { try { _cleanup(); } catch (e) { console.error(e); } }
  _cleanup = null;

  if (!m) { if (_notFound) _notFound(path); return; }

  const ctx = { params: m.params, query, navigate, route: m.route };
  try {
    const result = await m.route.mount(ctx);
    if (seq === _mountSeq && typeof result === 'function') _cleanup = result;
    else if (seq !== _mountSeq && typeof result === 'function') result(); // stale mount — clean immediately
  } catch (e) {
    console.error('[router] mount failed for', path, e);
    if (_onError) _onError(path, e);
  }
}

export function startRouter({ routes, beforeEach, notFound, onError }) {
  _routes = routes;
  _beforeEach = beforeEach || null;
  _notFound = notFound || null;
  _onError = onError || null;
  window.addEventListener('hashchange', handleChange);
  handleChange();
}
