import assert from "node:assert/strict";
import test from "node:test";

import { createSpectrumRuntime } from "../spectrum-runtime.mjs";

function runtimeHarness() {
  const imports = [];
  const configs = [];
  const localIMessage = { config: () => ({ provider: "local" }) };
  const imessage = { config: () => ({ provider: "cloud" }) };
  const core = {
    Spectrum: async (config) => {
      configs.push(config);
      return { messages: [] };
    },
    attachment: Symbol("attachment"),
    voice: Symbol("voice"),
    text: Symbol("text"),
    markdown: Symbol("markdown"),
    typing: Symbol("typing"),
  };
  const modules = {
    "@spectrum-ts/core": core,
    "@spectrum-ts/imessage-local": { localIMessage },
    "spectrum-ts/providers/imessage": { imessage },
  };
  const importer = async (specifier) => {
    imports.push(specifier);
    return modules[specifier];
  };
  return { configs, core, importer, imports };
}

test("local mode selects the dedicated local provider without cloud credentials", async () => {
  const harness = runtimeHarness();

  const runtime = await createSpectrumRuntime({
    localMode: true,
    projectId: "unused-id",
    projectSecret: "unused-secret",
    telemetry: false,
    importer: harness.importer,
  });

  assert.deepEqual(harness.imports, [
    "@spectrum-ts/core",
    "@spectrum-ts/imessage-local",
  ]);
  assert.deepEqual(harness.configs, [{
    providers: [{ provider: "local" }],
    options: { flattenGroups: true },
    telemetry: false,
  }]);
  assert.equal(runtime.attachment, harness.core.attachment);
});

test("cloud mode selects the managed provider and passes project credentials", async () => {
  const harness = runtimeHarness();

  await createSpectrumRuntime({
    localMode: false,
    projectId: "project-id",
    projectSecret: "project-secret",
    telemetry: true,
    importer: harness.importer,
  });

  assert.deepEqual(harness.imports, [
    "@spectrum-ts/core",
    "spectrum-ts/providers/imessage",
  ]);
  assert.deepEqual(harness.configs, [{
    providers: [{ provider: "cloud" }],
    options: { flattenGroups: true },
    telemetry: true,
    projectId: "project-id",
    projectSecret: "project-secret",
  }]);
});

test("installed Spectrum packages expose both provider APIs", async () => {
  const [core, local, cloud] = await Promise.all([
    import("@spectrum-ts/core"),
    import("@spectrum-ts/imessage-local"),
    import("spectrum-ts/providers/imessage"),
  ]);

  assert.equal(typeof core.Spectrum, "function");
  assert.equal(local.localIMessage.config().__name, "local_imessage");
  assert.equal(cloud.imessage.config().__name, "imessage");
});
