import assert from "node:assert/strict";
import { existsSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const harnessDir = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(harnessDir, "..", "..", "..");
const require = createRequire(pathToFileURL(join(repoRoot, "package.json")));

function failAbsentRuntime(message) {
  throw new Error(`declared Playwright runtime is absent: ${message}`);
}

let playwright;
try {
  playwright = require("@playwright/test");
} catch (error) {
  if (error && error.code === "MODULE_NOT_FOUND") {
    failAbsentRuntime("@playwright/test is not installed");
  }
  throw error;
}

const { chromium, expect } = playwright;
const chromiumPath = chromium.executablePath();
if (!chromiumPath || !existsSync(chromiumPath)) {
  failAbsentRuntime(`Chromium executable not found at ${chromiumPath || "<unset>"}`);
}

const { buildSync } = require("esbuild");
const bundlePath = join(
  repoRoot,
  "plugins",
  "kanban",
  "dashboard",
  "dist",
  "index.js",
);

const hostSource = String.raw`
import * as React from "react";
import { createRoot } from "react-dom/client";

function elementComponent(tag, omitted) {
  return function HostComponent(props) {
    const clean = {};
    const children = props.children;
    for (const key of Object.keys(props)) {
      if (key === "children" || omitted.indexOf(key) !== -1) continue;
      clean[key] = props[key];
    }
    if (tag === "button" && !clean.type) clean.type = "button";
    return React.createElement(tag, clean, children);
  };
}

const Card = elementComponent("div", []);
const CardContent = elementComponent("div", []);
const Badge = elementComponent("span", ["variant"]);
const Button = elementComponent("button", ["size", "variant"]);
const Input = elementComponent("input", []);
const Label = elementComponent("label", []);
const Select = elementComponent("select", ["onValueChange"]);
const SelectOption = elementComponent("option", []);

function deferred() {
  let resolvePromise;
  let rejectPromise;
  const promise = new Promise(function (resolve, reject) {
    resolvePromise = resolve;
    rejectPromise = reject;
  });
  return { promise: promise, resolve: resolvePromise, reject: rejectPromise };
}

const alphaSettingsGate = deferred();
const alphaProfilesGate = deferred();
let betaOrchestrationLoads = 0;
let betaAllowed = ["beta", "blocked"];
let pendingPut = null;
let putRecord = null;
const calls = [];

const boards = [
  { slug: "alpha", name: "Alpha", total: 1, counts: {} },
  { slug: "beta", name: "Beta", total: 1, counts: {} },
];
const emptyBoard = {
  columns: ["triage", "todo", "scheduled", "ready", "running", "blocked", "review", "done"].map(
    function (name) { return { name: name, tasks: [] }; }
  ),
  tenants: [],
  assignees: [],
  latest_event_id: 0,
  now: 1,
};

function alphaSettings() {
  return {
    board: "alpha",
    orchestrator_profile: "alpha-stale",
    default_assignee: "alpha-stale",
    auto_decompose: true,
    auto_promote_children: true,
    resolved_orchestrator_profile: "alpha-stale",
    resolved_default_assignee: "alpha-stale",
    active_profile: "alpha-stale",
    board_allowed_profiles: ["alpha-stale"],
    effective_allowed_profiles: ["alpha-stale"],
  };
}

function betaSettings() {
  const effective = betaAllowed.filter(function (name) { return name === "beta"; });
  return {
    board: "beta",
    orchestrator_profile: "",
    default_assignee: "",
    auto_decompose: true,
    auto_promote_children: false,
    resolved_orchestrator_profile: effective[0] || null,
    resolved_default_assignee: effective[0] || null,
    active_profile: "beta",
    board_allowed_profiles: betaAllowed.slice(),
    effective_allowed_profiles: effective,
  };
}

function alphaProfiles() {
  return {
    profiles: [{
      name: "alpha-stale",
      is_default: false,
      description: "stale alpha response",
      description_auto: false,
      skill_count: 0,
      machine_allowed: true,
      board_selected: true,
      effective_allowed: true,
    }],
  };
}

function betaProfiles() {
  return {
    profiles: [
      {
        name: "beta",
        is_default: false,
        description: "beta worker",
        description_auto: false,
        skill_count: 0,
        machine_allowed: true,
        board_selected: betaAllowed.indexOf("beta") !== -1,
        effective_allowed: betaAllowed.indexOf("beta") !== -1,
      },
      {
        name: "blocked",
        is_default: false,
        description: "historical blocked worker",
        description_auto: false,
        skill_count: 0,
        machine_allowed: false,
        board_selected: betaAllowed.indexOf("blocked") !== -1,
        effective_allowed: false,
      },
    ],
  };
}

function fetchJSON(url, options) {
  const request = options || {};
  const method = request.method || "GET";
  const parsed = new URL(url, window.location.origin);
  const board = parsed.searchParams.get("board");
  const body = request.body ? JSON.parse(request.body) : null;
  calls.push({ url: url, method: method, body: body });

  if (parsed.pathname.endsWith("/config")) {
    return Promise.resolve({
      render_markdown: true,
      lane_by_profile: false,
      include_archived_by_default: false,
    });
  }
  if (parsed.pathname.endsWith("/boards")) {
    return Promise.resolve({ boards: boards, current: "alpha" });
  }
  if (parsed.pathname.endsWith("/board")) {
    return Promise.resolve(emptyBoard);
  }
  if (parsed.pathname.endsWith("/profiles")) {
    if (board === "alpha") return alphaProfilesGate.promise;
    if (board === "beta") return Promise.resolve(betaProfiles());
  }
  if (parsed.pathname.endsWith("/orchestration")) {
    if (method === "PUT") {
      putRecord = { url: url, body: body };
      pendingPut = deferred();
      return pendingPut.promise.then(function () {
        betaAllowed = body.allowed_profiles.slice();
        return betaSettings();
      });
    }
    if (board === "alpha") return alphaSettingsGate.promise;
    if (board === "beta") {
      betaOrchestrationLoads += 1;
      if (betaOrchestrationLoads === 1) {
        return Promise.reject(new Error(
          '503: {"detail":"planned beta load failure"}'
        ));
      }
      return Promise.resolve(betaSettings());
    }
  }
  if (parsed.pathname.endsWith("/dispatch/nudge")) {
    return Promise.resolve({ ok: true });
  }
  return Promise.reject(new Error("unexpected harness request: " + method + " " + url));
}

class HarnessWebSocket {
  constructor() {
    const self = this;
    queueMicrotask(function () {
      if (self.onopen) self.onopen({});
    });
  }
  close() {}
}
window.WebSocket = HarnessWebSocket;

window.__KANBAN_HARNESS__ = {
  calls: calls,
  currentBoard: function () {
    return window.localStorage.getItem("hermes.kanban.selectedBoard");
  },
  resolveAlpha: function () {
    alphaSettingsGate.resolve(alphaSettings());
    alphaProfilesGate.resolve(alphaProfiles());
  },
  resolvePut: function () {
    if (!pendingPut) throw new Error("no orchestration PUT is pending");
    pendingPut.resolve();
  },
  putRecord: function () { return putRecord; },
};

window.__HERMES_PLUGIN_SDK__ = {
  React: React,
  components: {
    Card: Card,
    CardContent: CardContent,
    Badge: Badge,
    Button: Button,
    Input: Input,
    Label: Label,
    Select: Select,
    SelectOption: SelectOption,
  },
  hooks: {
    useState: React.useState,
    useEffect: React.useEffect,
    useCallback: React.useCallback,
    useMemo: React.useMemo,
    useRef: React.useRef,
  },
  utils: {
    cn: function () {
      return Array.prototype.slice.call(arguments).filter(Boolean).join(" ");
    },
    timeAgo: function () { return "now"; },
  },
  fetchJSON: fetchJSON,
  buildWsUrl: function () { return Promise.resolve("ws://kanban.test/events"); },
  useI18n: (function () {
    const value = { t: { kanban: null }, locale: "en" };
    return function () { return value; };
  })(),
};

window.__HERMES_PLUGINS__ = {
  register: function (name, Component) {
    if (name !== "kanban") throw new Error("unexpected plugin registration: " + name);
    window.__REGISTERED_KANBAN_COMPONENT__ = Component;
    createRoot(document.getElementById("root")).render(React.createElement(Component));
  },
};
`;

const hostBundle = buildSync({
  absWorkingDir: repoRoot,
  bundle: true,
  define: { "process.env.NODE_ENV": '"production"' },
  format: "iife",
  platform: "browser",
  stdin: {
    contents: hostSource,
    loader: "js",
    resolveDir: repoRoot,
    sourcefile: "dashboard-orchestration-harness-host.js",
  },
  target: "chrome120",
  write: false,
}).outputFiles[0].text;

async function main() {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  page.setDefaultTimeout(5_000);
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error));

  try {
    await page.route("http://kanban.test/**", async (route) => {
      await route.fulfill({
        body: "<!doctype html><html><body><div id=\"root\"></div></body></html>",
        contentType: "text/html",
        status: 200,
      });
    });
    await page.goto("http://kanban.test/");
    await page.evaluate(() => {
      window.localStorage.setItem("hermes.kanban.selectedBoard", "alpha");
    });
    await page.addScriptTag({ content: hostBundle });
    await page.addScriptTag({ path: bundlePath });

    const boardSwitcher = page.getByLabel("Switch kanban board");
    await expect(boardSwitcher).toBeVisible();
    await expect(boardSwitcher).toHaveValue("alpha");
    await boardSwitcher.selectOption("beta");
    await expect(boardSwitcher).toHaveValue("beta");
    await expect.poll(
      () => page.evaluate(() => window.__KANBAN_HARNESS__.currentBoard())
    ).toBe("beta");

    await page.getByRole("button", { name: "▸ Orchestration settings" }).evaluate(
      (button) => button.click()
    );
    await expect(page.getByText(
      "Failed to load orchestration settings: planned beta load failure"
    )).toBeVisible();

    await page.getByRole("button", { name: "Reload" }).click();
    await expect(page.getByText("Profiles allowed on this board")).toBeVisible();
    await expect(page.getByText(
      "Failed to load orchestration settings: planned beta load failure"
    )).toHaveCount(0);

    const blockedCheckbox = page.getByRole("checkbox", {
      name: "blocked — blocked by machine policy",
    });
    await expect(blockedCheckbox).toBeVisible();
    await expect(blockedCheckbox).toBeDisabled();
    await expect(page.getByText("blocked by machine policy", { exact: true })).toBeVisible();

    const betaCheckbox = page.getByRole("checkbox", {
      name: "Allow beta to run work on this board",
    });
    await expect(betaCheckbox).toBeVisible();
    await expect(betaCheckbox).toBeEnabled();
    await expect(betaCheckbox).toBeChecked();

    await page.evaluate(() => window.__KANBAN_HARNESS__.resolveAlpha());
    await page.evaluate(() => new Promise((resolvePromise) => setTimeout(resolvePromise, 0)));
    await expect(page.getByText("alpha-stale", { exact: true })).toHaveCount(0);
    await expect(betaCheckbox).toBeVisible();
    await expect(betaCheckbox).toBeChecked();

    await betaCheckbox.click();
    await expect.poll(
      () => page.evaluate(() => window.__KANBAN_HARNESS__.putRecord())
    ).not.toBeNull();
    const putRecord = await page.evaluate(() => window.__KANBAN_HARNESS__.putRecord());
    assert.deepEqual(putRecord, {
      url: "/api/plugins/kanban/orchestration?board=beta",
      body: { allowed_profiles: [] },
    });
    await expect(betaCheckbox).toBeDisabled();
    await expect(page.getByLabel("Inherit machine policy")).toBeDisabled();

    await page.evaluate(() => window.__KANBAN_HARNESS__.resolvePut());
    await expect(page.getByText("No workers can run on this board.")).toBeVisible();
    await expect(betaCheckbox).toBeEnabled();
    await expect(betaCheckbox).not.toBeChecked();
    await expect(page.getByText("Settings saved.")).toBeVisible();

    assert.equal(pageErrors.length, 0, pageErrors.map((error) => error.stack).join("\n"));
    process.stdout.write(JSON.stringify({
      ok: true,
      board: "beta",
      put_url: putRecord.url,
      put_body: putRecord.body,
      checks: 6,
    }) + "\n");
  } finally {
    await browser.close();
  }
}

main().catch((error) => {
  process.stderr.write(`${error && error.stack ? error.stack : error}\n`);
  process.exitCode = 1;
});
