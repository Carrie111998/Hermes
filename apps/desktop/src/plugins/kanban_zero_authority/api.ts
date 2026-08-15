import type { EvidenceVector, PublicationIntent } from "./types";

export class KanbanSecurityApi {
  constructor(
    private readonly baseUrl: string,
    private readonly fetcher: typeof fetch = fetch,
  ) {}

  async publications(): Promise<PublicationIntent[]> {
    const response = await this.fetcher(`${this.baseUrl}/api/kanban/security/publications`, {
      credentials: "include",
      cache: "no-store",
    });
    if (!response.ok) throw new Error(`publication queue failed: ${response.status}`);
    return response.json();
  }

  async decide(
    intentId: string,
    wireSha256: string,
    decision: "approve" | "reject",
    reason?: string,
  ): Promise<{ approval_id: string }> {
    const response = await this.fetcher(
      `${this.baseUrl}/api/kanban/security/publications/${encodeURIComponent(intentId)}/decision`,
      {
        method: "POST",
        credentials: "include",
        cache: "no-store",
        headers: {
          "Content-Type": "application/json",
          "If-Match": `"${wireSha256}"`,
        },
        body: JSON.stringify({ wire_sha256: wireSha256, decision, reason }),
      },
    );
    if (!response.ok) throw new Error(`publication decision failed: ${response.status}`);
    return response.json();
  }

  async evidence(taskId: string, runId: number): Promise<EvidenceVector> {
    const response = await this.fetcher(
      `${this.baseUrl}/api/kanban/security/tasks/${encodeURIComponent(taskId)}/runs/${runId}/evidence`,
      { credentials: "include", cache: "no-store" },
    );
    if (!response.ok) throw new Error(`evidence read failed: ${response.status}`);
    return response.json();
  }
}
