export function createBaileysVersionResolver({
  fetchVersion,
  fallbackVersion,
  timeoutMs = 5000,
  setTimer = setTimeout,
  clearTimer = clearTimeout,
  onFallback = () => {},
}) {
  let resolution = null;

  return function resolveVersion() {
    if (resolution !== null) {
      return resolution;
    }

    resolution = new Promise((resolve) => {
      let settled = false;
      let timer = null;

      const finish = (version, fallbackReason = null) => {
        if (settled) return;
        settled = true;
        if (timer !== null) {
          clearTimer(timer);
        }
        if (fallbackReason !== null) {
          try {
            onFallback(fallbackReason);
          } catch {}
        }
        resolve(version);
      };

      // Baileys' helper has no abort-signal input. This bounds bridge startup
      // latency; the one process-cached lookup may settle later but cannot
      // accumulate across sweep reconnects.
      timer = setTimer(() => finish(fallbackVersion, 'timeout'), timeoutMs);
      Promise.resolve()
        .then(fetchVersion)
        .then(
          (result) => {
            const version = result?.version;
            const validVersion = Array.isArray(version)
              && version.length === 3
              && version.every((part) => Number.isSafeInteger(part) && part >= 0);
            if (result?.isLatest === true && validVersion) {
              finish(version);
              return;
            }
            finish(
              fallbackVersion,
              result?.error || (validVersion ? 'not_latest' : 'invalid_version'),
            );
          },
          (error) => finish(fallbackVersion, error),
        );
    });

    return resolution;
  };
}
