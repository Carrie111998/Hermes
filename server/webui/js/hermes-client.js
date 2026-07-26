import { getSession } from './session.js';

const SESSION_KEY = 'ifz-hermes-dashboard-session';

let _hermesAvailable = null;
let _hermesCheckPromise = null;

function apiUrl(path) {
  return new URL(path, document.baseURI || location.href).href;
}

export async function isHermesAvailable() {
  if (_hermesAvailable !== null) return _hermesAvailable;
  if (!_hermesCheckPromise) {
    _hermesCheckPromise = (async () => {
      try {
        const res = await fetch(apiUrl('/health'), { credentials: 'include' });
        let health = null;
        try { health = await res.json(); } catch { /* non-json */ }
        _hermesAvailable = res.ok && health?.chat_enabled === true;
      } catch {
        _hermesAvailable = false;
      }
      return _hermesAvailable;
    })();
  }
  return _hermesCheckPromise;
}

export async function hermesApi(path, { method = 'GET', body } = {}) {
  const opts = {
    method,
    headers: { Accept: 'application/json' },
    credentials: 'include',
  };
  const session = getSession();
  if (session?.token) opts.headers.Authorization = `Bearer ${session.token}`;
  if (session?.company?.id) opts.headers['X-Company-ID'] = session.company.id;
  if (body != null) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const token = window.__HERMES_CONFIG__?.csrfToken;
  if (token && /^(POST|PUT|PATCH|DELETE)$/i.test(method)) {
    opts.headers['X-Hermes-CSRF-Token'] = token;
  }
  const res = await fetch(apiUrl(path), opts);
  let payload = null;
  try { payload = await res.json(); } catch { /* non-json */ }
  if (!res.ok) {
    const detail = typeof payload?.detail === 'string'
      ? payload.detail
      : payload?.detail?.message || payload?.detail?.[0]?.msg;
    const err = new Error(payload?.error || payload?.message || detail || `Hermes ${method} ${path} failed (${res.status})`);
    err.status = res.status;
    throw err;
  }
  return payload;
}

export async function ensureDashboardSession() {
  const stored = sessionStorage.getItem(SESSION_KEY);
  if (stored) {
    try {
      const data = await hermesApi(`/api/session?session_id=${encodeURIComponent(stored)}&messages=0&resolve_model=0`);
      if (data?.session?.session_id) return data.session;
    } catch (e) {
      if (e.status !== 404) throw e;
      sessionStorage.removeItem(SESSION_KEY);
    }
  }
  const data = await hermesApi('/api/session/new', {
    method: 'POST',
    body: { profile: 'default' },
  });
  const sid = data?.session?.session_id;
  if (!sid) throw new Error('Hermes did not return a session id');
  sessionStorage.setItem(SESSION_KEY, sid);
  return data.session;
}

export function streamChatResponse(streamId, { onToken, onDone, onError, signal } = {}) {
  return new Promise((resolve, reject) => {
    const source = new EventSource(
      apiUrl(`/api/chat/stream?stream_id=${encodeURIComponent(streamId)}`),
      { withCredentials: true },
    );
    let text = '';
    let settled = false;

    const cleanup = () => {
      try { source.close(); } catch { /* already closed */ }
    };

    const finish = (result) => {
      if (settled) return;
      settled = true;
      cleanup();
      resolve(result);
    };

    const fail = (err) => {
      if (settled) return;
      settled = true;
      cleanup();
      reject(err);
    };

    if (signal) {
      if (signal.aborted) {
        fail(new Error('aborted'));
        return;
      }
      signal.addEventListener('abort', () => fail(new Error('aborted')), { once: true });
    }

    source.addEventListener('token', (e) => {
      try {
        const chunk = JSON.parse(e.data).text || '';
        text += chunk;
        onToken?.(text, chunk);
      } catch { /* ignore malformed token */ }
    });

    source.addEventListener('done', (e) => {
      try {
        const d = JSON.parse(e.data);
        if (d.answer && !text) text = d.answer;
      } catch { /* ignore */ }
      onDone?.(text);
      finish(text);
    });

    source.addEventListener('apperror', (e) => {
      let msg = 'Agent error';
      try { msg = JSON.parse(e.data).message || msg; } catch { /* ignore */ }
      onError?.(msg);
      fail(new Error(msg));
    });

    source.addEventListener('cancel', () => finish(text));

    source.onerror = () => {
      if (settled) return;
      if (text) finish(text);
      else fail(new Error('Stream connection lost'));
    };
  });
}

export async function askHermes(question, { context, onToken, onDone, onError, signal } = {}) {
  const session = await ensureDashboardSession();
  let message = question;
  if (context) message = `${context}\n\n${question}`;
  const startData = await hermesApi('/api/chat/start', {
    method: 'POST',
    body: {
      session_id: session.session_id,
      message,
      model: session.model || '',
      workspace: session.workspace,
      model_provider: session.model_provider,
      profile: session.profile || 'default',
    },
  });
  const streamId = startData?.stream_id;
  if (!streamId) throw new Error('Hermes did not return a stream id');
  return streamChatResponse(streamId, { onToken, onDone, onError, signal });
}
