export const PENDING_APPROVAL_REFRESH_MS = 10_000;

type PendingApprovalResult = {
  count: number;
};

type IntervalHandle = unknown;

interface PendingApprovalPollingOptions {
  load: () => Promise<PendingApprovalResult>;
  onCount: (count: number) => void;
  onError?: (error: unknown) => void;
  setIntervalFn?: (
    callback: () => void,
    delay: number,
  ) => IntervalHandle;
  clearIntervalFn?: (handle: IntervalHandle) => void;
}

export interface PendingApprovalPollingHandle {
  initial: Promise<void>;
  refresh: () => Promise<void>;
  stop: () => void;
}

/**
 * Start bounded polling for the profile-scoped pending approval count.
 * Failures leave the last known value untouched; stopping suppresses late
 * promise resolutions so profile switches cannot paint stale counts.
 */
export function startPendingApprovalPolling({
  load,
  onCount,
  onError,
  setIntervalFn = (callback, delay) => globalThis.setInterval(callback, delay),
  clearIntervalFn = (handle) =>
    globalThis.clearInterval(
      handle as ReturnType<typeof globalThis.setInterval>,
    ),
}: PendingApprovalPollingOptions): PendingApprovalPollingHandle {
  let active = true;
  let loading = false;

  const refresh = async () => {
    if (!active || loading) return;
    loading = true;
    try {
      const result = await load();
      if (active) onCount(Math.max(0, result.count));
    } catch (error) {
      if (active) onError?.(error);
    } finally {
      loading = false;
    }
  };

  const initial = refresh();
  const interval = setIntervalFn(
    () => void refresh(),
    PENDING_APPROVAL_REFRESH_MS,
  );

  return {
    initial,
    refresh,
    stop: () => {
      if (!active) return;
      active = false;
      clearIntervalFn(interval);
    },
  };
}
