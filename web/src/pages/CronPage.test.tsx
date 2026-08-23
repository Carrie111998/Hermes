import { describe, expect, it, vi } from "vitest";

import {
  cronJobSummaryPresentation,
  loadCronJobDetailForEditor,
} from "@/lib/cron-job";
import type { CronJob } from "@/lib/api";

const summary: CronJob = {
  id: "job-detail-1",
  name: "summary job",
  enabled: true,
  mode: "agent",
  delivery_kind: "local",
};

describe("loadCronJobDetailForEditor", () => {
  it("fails closed when no concrete profile is selected", async () => {
    const getDetail = vi.fn();

    await expect(
      loadCronJobDetailForEditor(getDetail, summary, "all"),
    ).rejects.toThrow("cron_detail_profile_required");
    expect(getDetail).not.toHaveBeenCalled();
  });

  it("loads editable configuration from the explicit profile detail endpoint", async () => {
    const detail: CronJob = {
      ...summary,
      prompt: "private editable prompt",
      workdir: "/private/edit-worktree",
    };
    const getDetail = vi.fn().mockResolvedValue(detail);

    await expect(
      loadCronJobDetailForEditor(getDetail, summary, "worker_alpha"),
    ).resolves.toEqual(detail);
    expect(getDetail).toHaveBeenCalledOnce();
    expect(getDetail).toHaveBeenCalledWith("job-detail-1", "worker_alpha");
  });

  it("presents summaries without falling back to private config", () => {
    const accidentalWideResponse: CronJob = {
      ...summary,
      name: undefined,
      prompt: "PRIVATE_PROMPT",
      script: "PRIVATE_SCRIPT",
      provider: "PRIVATE_PROVIDER",
      model: "PRIVATE_MODEL",
      mode: "monitor",
      model_configured: true,
    };

    expect(cronJobSummaryPresentation(accidentalWideResponse)).toEqual({
      title: "job-detail-1",
      mode: "monitor",
      modelLabel: "configured",
    });
  });
});
