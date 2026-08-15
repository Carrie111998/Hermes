export type EvidenceDimension = "present" | "absent" | "unknown";
export type ProcessState = "alive" | "dead" | "unknown";
export type MotionState = "active" | "idle" | "unknown";
export type CoverageState = "strong" | "best_effort" | "unknown";
export type ReclaimDecision =
  | "preserve"
  | "eligible_dead"
  | "eligible_inert"
  | "unknown";

export interface EvidenceVector {
  process: ProcessState;
  motion: MotionState;
  artifacts: EvidenceDimension;
  publication: EvidenceDimension;
  coverage: CoverageState;
  decision: ReclaimDecision;
  reason_codes: string[];
  observation_ids: string[];
}

export interface PublicationIntent {
  intent_id: string;
  task_id: string;
  run_id: number;
  kind: string;
  state: string;
  publisher_principal: string;
  adapter_version: string;
  target: Record<string, unknown>;
  marker: string;
  wire_sha256: string;
  created_at: number;
}
