const MESSAGE_TYPE = 'interfaze:oauth';
const MESSAGE_STATUSES = new Set(['connected', 'cancelled', 'failed']);

const defaultTimerApi = {
  setInterval: (...args) => globalThis.setInterval(...args),
  clearInterval: id => globalThis.clearInterval(id),
  setTimeout: (...args) => globalThis.setTimeout(...args),
  clearTimeout: id => globalThis.clearTimeout(id),
};

export function startEmailOAuth({
  provider,
  startOAuth,
  listIntegrations,
  onConnected,
  onStatus,
  windowRef = window,
  timerApi = defaultTimerApi,
  pollMs = 1000,
}) {
  const popup = windowRef.open(
    '',
    `interfaze-oauth-${provider}`,
    'popup=yes,width=620,height=760',
  );
  if (!popup) {
    onStatus({ status: 'blocked' });
    return null;
  }

  let settled = false;
  let polling = false;
  let pollTimer = null;
  let expiryTimer = null;
  let resolveReady;
  const ready = new Promise(resolve => { resolveReady = resolve; });

  function cleanup() {
    windowRef.removeEventListener('message', receiveMessage);
    if (pollTimer !== null) timerApi.clearInterval(pollTimer);
    if (expiryTimer !== null) timerApi.clearTimeout(expiryTimer);
    pollTimer = null;
    expiryTimer = null;
  }

  function stopSilently() {
    if (settled) return;
    settled = true;
    cleanup();
  }

  function finish(status, error = undefined, { closePopup = false } = {}) {
    if (settled) return;
    settled = true;
    cleanup();
    if (closePopup && !popup.closed) popup.close();
    if (status === 'connected') onConnected();
    else onStatus(error === undefined ? { status } : { status, error });
  }

  function receiveMessage(event) {
    const data = event.data;
    if (event.origin !== windowRef.location.origin
        || event.source !== popup
        || !data
        || data.type !== MESSAGE_TYPE
        || data.provider !== provider
        || !MESSAGE_STATUSES.has(data.status)) return;
    finish(data.status, undefined, { closePopup: data.status === 'connected' });
  }

  async function poll() {
    if (settled || polling) return;
    polling = true;
    try {
      const result = await listIntegrations();
      const connected = result.items?.some(
        item => item.provider === provider && item.status === 'connected',
      );
      if (connected) finish('connected', undefined, { closePopup: true });
      else if (popup.closed) finish('cancelled');
    } catch {
      // A transient list failure must not abort an authorization in progress.
    } finally {
      polling = false;
    }
  }

  windowRef.addEventListener('message', receiveMessage);
  Promise.resolve().then(async () => {
    try {
      const result = await startOAuth(provider);
      if (settled) {
        resolveReady(false);
        return;
      }
      if (popup.closed) {
        finish('cancelled');
        resolveReady(false);
        return;
      }
      const expiresIn = Number(result?.expires_in);
      if (typeof result?.authorize_url !== 'string'
          || result.authorize_url.length === 0
          || !Number.isFinite(expiresIn)
          || expiresIn <= 0) {
        throw new Error('OAuth start returned an invalid response');
      }
      popup.location.replace(result.authorize_url);
      pollTimer = timerApi.setInterval(poll, pollMs);
      expiryTimer = timerApi.setTimeout(
        () => finish('expired'),
        expiresIn * 1000,
      );
      resolveReady(true);
    } catch (error) {
      finish('start_failed', error, { closePopup: true });
      resolveReady(false);
    }
  });

  return {
    popup,
    ready,
    cancel({ notify = false } = {}) {
      if (notify) finish('cancelled');
      else stopSilently();
    },
  };
}
