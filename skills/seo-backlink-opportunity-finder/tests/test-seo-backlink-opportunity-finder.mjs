import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { promisify } from "node:util";
import { test } from "node:test";
import {
  LANES,
  isUnsafeLiteralHost,
  tierRequirements,
  validate,
} from "../scripts/validate-opportunity-ledger.mjs";

const execFileAsync = promisify(execFile);
const skillRoot = fileURLToPath(new URL("..", import.meta.url));
const script = path.join(skillRoot, "scripts", "validate-opportunity-ledger.mjs");

function candidates(count = 40) {
  const lanes = [...LANES];
  return Array.from({ length: count }, (_, index) => {
    const host = `prospect-${index + 1}.example.com`;
    const url = `https://${host}/opportunity`;
    return {
      id: `candidate-${index + 1}`,
      target_url: url,
      root_domain: host,
      lane: lanes[index % 8],
      route: "editorial_pitch",
      evidence_state: "research_lead",
      evidence_url: url,
      why_relevant: "Deterministic test record.",
      next_action: "Verify before outreach.",
      cost_or_disclosure: "Unknown until verified.",
      quality_risk: "Requires manual review.",
    };
  });
}

test("accepts a complete minimum tier and preserves full-tier expectations", () => {
  const ledger = { candidates: candidates() };
  assert.equal(validate(ledger, { tier: "minimum" }).ok, true);
  const full = validate(ledger, { tier: "full" });
  assert.equal(full.ok, false);
  assert.match(full.errors.join("\n"), /at least 100 candidates/);
  assert.deepEqual(tierRequirements("minimum"), { tier: "minimum", minimumCandidates: 40, minimumDomains: 25, minimumLanes: 8 });
});

test("rejects private literal addresses and unsupported ledger enums", () => {
  assert.equal(isUnsafeLiteralHost("172.16.0.1"), true);
  assert.equal(isUnsafeLiteralHost("[::1]"), true);
  assert.equal(isUnsafeLiteralHost("203.0.113.7"), true);
  assert.equal(isUnsafeLiteralHost("93.184.216.34"), false);

  const unsafe = candidates();
  unsafe[0].target_url = "https://172.16.0.1/opportunity";
  unsafe[0].evidence_url = "https://172.16.0.1/evidence";
  unsafe[0].root_domain = "172.16.0.1";
  assert.match(validate({ candidates: unsafe }, { tier: "minimum" }).errors.join("\n"), /safe public http\(s\) URL/);

  const invalidEnum = candidates();
  invalidEnum[0].lane = "almost_editorial";
  assert.match(validate({ candidates: invalidEnum }, { tier: "minimum" }).errors.join("\n"), /approved coverage matrix/);
});

test("prints discoverable help without network access", async () => {
  const { stdout } = await execFileAsync(process.execPath, [script, "--help"], { windowsHide: true });
  assert.match(stdout, /--tier full\|minimum/);
});

test("validates the selected tier through the CLI without network access", async () => {
  const temp = await mkdtemp(path.join(os.tmpdir(), "backlink-skill-test-"));
  const input = path.join(temp, "opportunities.json");
  try {
    await writeFile(input, JSON.stringify({ candidates: candidates() }), "utf8");
    const { stdout } = await execFileAsync(process.execPath, [script, "--input", input, "--tier", "minimum"], { windowsHide: true });
    const result = JSON.parse(stdout);
    assert.equal(result.ok, true);
    assert.equal(result.summary.tier, "minimum");
  } finally {
    await rm(temp, { recursive: true, force: true });
  }
});
