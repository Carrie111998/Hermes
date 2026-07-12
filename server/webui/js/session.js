/* Session persistence (sessionStorage only — mock DB itself is reseeded per load). */

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

export function clearSession() {
  try { sessionStorage.removeItem(KEY); } catch { /* ignore */ }
}

export function isAuthed() {
  return !!getSession()?.token;
}
