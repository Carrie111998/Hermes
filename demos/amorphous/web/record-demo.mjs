/* Hermes Station demo video — drives the real UI end-to-end with Playwright,
   recording video. Run: node record-demo.mjs */
import { chromium } from "playwright";

const BASE = "http://127.0.0.1:8877";
const USER = "demovideo" + Date.now().toString().slice(-5);
const pause = (ms) => new Promise((r) => setTimeout(r, ms));

const browser = await chromium.launch();
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 860 },
  recordVideo: { dir: "/tmp/station-video", size: { width: 1440, height: 860 } },
});
const page = await ctx.newPage();
page.setDefaultTimeout(30000);

const log = (m) => console.log(new Date().toISOString().slice(11, 19), m);

try {
  /* ============ 1. ONBOARDING ============ */
  log("onboarding");
  await page.goto(`${BASE}/?user=${USER}`);
  await page.waitForSelector("text=Starting point", { timeout: 20000 });
  await pause(2200);

  // browse templates
  await page.click("text=Trader");
  await pause(1200);
  await page.click("text=Executive");
  await pause(1200);
  await page.click("text=Trader");
  await pause(900);

  // type a short brief slowly (visible typing)
  const brief = page.locator("textarea");
  await brief.click();
  await brief.pressSequentially(
    "I trade BTC and SOL. I want prices, charts and market news at a glance.",
    { delay: 28 });
  await pause(1200);

  await page.click("text=Build my Station");
  log("building (agent customizes from brief)…");
  await page.waitForSelector("text=COMPONENTS", { timeout: 180000 });
  await pause(3500);

  /* ============ 2. TOUR: sidebar / stats / cards ============ */
  log("tour");
  await page.mouse.move(120, 300);
  await pause(1500);

  /* ============ 3. DRAG a card ============ */
  log("drag");
  const handles = page.locator(".drag-handle");
  const n = await handles.count();
  if (n >= 2) {
    const src = await handles.nth(1).boundingBox();
    if (src) {
      await page.mouse.move(src.x + src.width / 2, src.y + src.height / 2);
      await pause(600);
      await page.mouse.down();
      await page.mouse.move(src.x + src.width / 2 + 60, src.y + 80, { steps: 12 });
      await page.mouse.move(src.x - 260, src.y + 260, { steps: 30 });
      await pause(500);
      await page.mouse.up();
      await pause(2000);
    }
  }

  /* ============ 4. RESIZE a card ============ */
  log("resize");
  const firstCard = page.locator(".react-grid-item").first();
  let bb = await firstCard.boundingBox();
  if (bb) {
    await page.mouse.move(bb.x + bb.width - 6, bb.y + bb.height - 6);
    await pause(600);
    await page.mouse.down();
    await page.mouse.move(bb.x + bb.width + 130, bb.y + bb.height + 60, { steps: 20 });
    await pause(400);
    await page.mouse.up();
    await pause(2000);
  }

  /* ============ 5. POP OUT a card (dialog with tabs) ============ */
  log("popout card");
  const chartHandle = page.locator(".drag-handle").nth(1);
  await chartHandle.dblclick();
  await page.waitForSelector('[role="dialog"]');
  await pause(2800);
  // switch to Ask Hermes tab briefly (dialog-scoped selectors)
  const dlg = page.locator('[role="dialog"]');
  await dlg.locator('[role="tab"]:has-text("Ask Hermes")').click();
  await pause(1800);
  await dlg.locator('[role="tab"]:has-text("View")').click();
  await pause(1500);
  await page.keyboard.press("Escape");
  await pause(1200);

  /* ============ 6. COMPONENT scoped chat ============ */
  log("scoped chat");
  const card0 = page.locator(".card-surface").first();
  await card0.hover();
  await pause(700);
  const askBtn = card0.locator("button:has(svg.lucide-message-circle)").first();
  await askBtn.click();
  await pause(1000);
  const scopedInput = page.locator('input[placeholder="Ask or change this component…"]');
  if (await scopedInput.count()) {
    await scopedInput.pressSequentially("What am I looking at here?", { delay: 25 });
    await pause(700);
    await page.keyboard.press("Enter");
    // wait for a reply (scoped agent)
    await pause(14000);
    // close overlay
    const overlay = page.locator(".card-surface .absolute.inset-0").first();
    const closeBtn = overlay.locator("button").last();
    if (await closeBtn.count()) await closeBtn.click().catch(() => {});
    await pause(800);
  }

  /* ============ 7. MAIN CHAT: live agent edit ============ */
  log("agent live edit");
  const mainInput = page.locator('input[placeholder*="reshape your dashboard"]');
  await mainInput.click();
  await mainInput.pressSequentially("Add a Dogecoin 7 day chart to my board", { delay: 26 });
  await pause(600);
  await page.keyboard.press("Enter");
  // agent runs with station tools; board updates via SSE
  await page.waitForSelector("text=DOGE", { timeout: 150000 }).catch(() => {});
  await pause(4000);

  /* ============ 8. POP OUT the chat window & drag it ============ */
  log("popout chat");
  const popBtn = page.locator("button:has(svg.lucide-picture-in-picture-2)").first();
  await popBtn.click();
  await pause(1200);
  const floatHead = page.locator("text=floating — drag me anywhere");
  const fb = await floatHead.boundingBox();
  if (fb) {
    await page.mouse.move(fb.x + 60, fb.y + 5);
    await page.mouse.down();
    await page.mouse.move(fb.x - 500, fb.y - 260, { steps: 28 });
    await page.mouse.up();
    await pause(1800);
  }
  // dock it back
  const dockBtn = page.locator("button:has(svg.lucide-panel-bottom)").first();
  if (await dockBtn.count()) { await dockBtn.click(); await pause(1500); }

  /* ============ 9. EVOLUTION: curator proposal flow ============ */
  log("evolve");
  await page.click("text=Evolve now");
  await pause(9000); // curator pass (heuristics on fresh telemetry)
  const tray = page.locator("text=Evolution proposals");
  if (await tray.count()) {
    await pause(2000);
    const tryBtn = page.locator("button:has-text('Try it')").first();
    if (await tryBtn.count()) {
      await tryBtn.click();
      await pause(3500); // preview mode
      const keep = page.locator("button:has-text('Keep')").first();
      if (await keep.count()) { await keep.click(); await pause(2500); }
    }
  }

  /* ============ 10. INSPECTOR beat ============ */
  log("inspector");
  const insp = page.locator("text=station coverage");
  if (!(await insp.count())) {
    await page.click("text=Inspector");
  }
  await pause(3000);

  log("done");
} catch (e) {
  console.error("DEMO ERROR:", e.message);
} finally {
  await pause(1500);
  await ctx.close(); // flushes video
  await browser.close();
}
console.log("VIDEO_DIR=/tmp/station-video");
