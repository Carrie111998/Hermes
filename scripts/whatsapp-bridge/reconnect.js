export function withTimeout(promise, timeoutMs, label, timers = globalThis) {
  let timeoutId;
  const timeout = new Promise((_, reject) => {
    timeoutId = timers.setTimeout(
      () => reject(new Error(`${label} timed out after ${timeoutMs}ms`)),
      timeoutMs,
    );
  });

  return Promise.race([promise, timeout]).finally(() => {
    timers.clearTimeout(timeoutId);
  });
}

export function createCachedVersionResolver(
  discoverVersion,
  { timeoutMs = 10_000, onFallback = () => {} } = {},
) {
  let cachedVersion;
  let hasResolved = false;

  return async function resolveVersion() {
    if (hasResolved) return cachedVersion;

    try {
      const result = await withTimeout(
        Promise.resolve().then(discoverVersion),
        timeoutMs,
        'Baileys version discovery',
      );
      cachedVersion = Array.isArray(result?.version) ? result.version : null;
    } catch (error) {
      cachedVersion = null;
      onFallback(error);
    }
    hasResolved = true;
    return cachedVersion;
  };
}

export function createConnectionWatchdog({
  setTimer = setTimeout,
  clearTimer = clearTimeout,
} = {}) {
  let timerId = null;

  return {
    arm(onTimeout, timeoutMs) {
      if (timerId !== null) clearTimer(timerId);
      timerId = setTimer(() => {
        timerId = null;
        onTimeout();
      }, timeoutMs);
    },
    cancel() {
      if (timerId === null) return;
      clearTimer(timerId);
      timerId = null;
    },
  };
}

export function reconnectDelayForReason(reason) {
  return reason === 515 ? 1000 : 3000;
}

export function createReconnectScheduler({
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  onError,
} = {}) {
  let timerId = null;
  let inFlight = false;
  let queuedRetry = null;

  const schedule = (start, delayMs) => {
    if (timerId !== null) return false;
    if (inFlight) {
      if (queuedRetry === null) queuedRetry = { start, delayMs };
      return false;
    }
    timerId = setTimer(async () => {
      timerId = null;
      inFlight = true;
      try {
        await start();
      } catch (error) {
        onError?.(error);
      } finally {
        inFlight = false;
        const retry = queuedRetry;
        queuedRetry = null;
        if (retry !== null) schedule(retry.start, retry.delayMs);
      }
    }, delayMs);
    return true;
  };

  return {
    schedule,
    cancel() {
      queuedRetry = null;
      if (timerId === null) return;
      clearTimer(timerId);
      timerId = null;
    },
  };
}
