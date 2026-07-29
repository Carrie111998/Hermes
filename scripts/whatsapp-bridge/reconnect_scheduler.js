export function createReconnectScheduler({
  start,
  retryDelayMs = 3000,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  onError = () => {},
}) {
  let timer = null;
  let timerDelay = null;
  let starting = false;
  let pendingDelay = null;

  function schedule(delayMs) {
    if (starting) {
      pendingDelay = pendingDelay === null
        ? delayMs
        : Math.min(pendingDelay, delayMs);
      return;
    }

    if (timer !== null) {
      if (timerDelay <= delayMs) {
        return;
      }
      clearTimer(timer);
    }
    timerDelay = delayMs;
    timer = setTimer(async () => {
      timer = null;
      timerDelay = null;
      starting = true;
      let failed = false;
      try {
        await start();
      } catch (error) {
        failed = true;
        try {
          onError(error);
        } catch {}
      } finally {
        starting = false;
        const requestedDelay = pendingDelay;
        pendingDelay = null;
        // If start succeeds, it owns the live socket and requests made while
        // it was starting are stale. A later close schedules a fresh attempt.
        if (failed) {
          schedule(requestedDelay === null
            ? retryDelayMs
            : Math.min(requestedDelay, retryDelayMs));
        }
      }
    }, delayMs);
  }

  return { schedule };
}
