import { useEffect, useState } from "react";

import { useProfileScope } from "@/contexts/useProfileScope";
import { api } from "@/lib/api";
import { startPendingApprovalPolling } from "@/lib/governance-pending";
import { cn } from "@/lib/utils";

export function GovernancePendingBadge({ collapsed }: { collapsed: boolean }) {
  const { profile } = useProfileScope();
  const [snapshot, setSnapshot] = useState({ profile, count: 0 });
  const count = snapshot.profile === profile ? snapshot.count : 0;

  useEffect(() => {
    const polling = startPendingApprovalPolling({
      load: api.getGovernanceApprovals,
      onCount: (nextCount) => setSnapshot({ profile, count: nextCount }),
    });
    return polling.stop;
  }, [profile]);

  if (count < 1) return null;

  const displayCount = count > 99 ? "99+" : String(count);
  const label = `${count} pending approval${count === 1 ? "" : "s"}`;

  return (
    <span
      aria-label={label}
      title={label}
      className={cn(
        "ml-auto inline-flex min-w-5 shrink-0 items-center justify-center rounded-full bg-warning px-1.5 py-0.5 font-sans text-[0.625rem] font-bold leading-none tracking-normal text-black",
        collapsed && "lg:absolute lg:right-1 lg:top-1 lg:min-w-3.5 lg:px-1 lg:py-px lg:text-[0.5rem]",
      )}
    >
      {displayCount}
    </span>
  );
}
