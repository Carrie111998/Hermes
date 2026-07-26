/* Auth/session persistence is tab-scoped so tokens do not leak across browser sessions. */

const KEY = 'ifz.auth';

export function getSession() {
  try {
    const raw = sessionStorage.getItem(KEY);
    return raw ? JSON.parse(raw) : null;
  } catch { return null; }
}

export function setSession(session) {
  try { sessionStorage.setItem(KEY, JSON.stringify(session)); } catch { /* ignore */ }
}

export function updateSession(patch) {
  const current = getSession() || {};
  const next = { ...current, ...(patch || {}) };
  setSession(next);
  return next;
}

export function clearSession() {
  try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
}

export function isAuthed() {
  return !!getSession()?.token;
}

export function homeRoute(session = getSession()) {
  return session?.user?.role === 'admin' ? '/admin/dashboard' : '/app/today';
}
