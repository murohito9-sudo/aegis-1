import { createServer } from "node:http";
import { mkdir, readFile } from "node:fs/promises";
import { chromium } from "playwright";

const html = await readFile("static/index.html", "utf8");
const server = createServer((req, res) => {
  if (req.url === "/" || req.url.startsWith("/?")) {
    res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
    res.end(html.replace("__AEGIS_CSRF__", "browser-test-token"));
    return;
  }
  res.writeHead(404);
  res.end("not found");
});
await new Promise(resolve => server.listen(0, "127.0.0.1", resolve));
const { port } = server.address();
const base = `http://127.0.0.1:${port}`;
const browser = await chromium.launch({ headless: true, args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 }, deviceScaleFactor: 1 });
const pageErrors = [];
page.on("pageerror", error => pageErrors.push(error.message));

const state = {
  ts: 1, uptime_s: 60, symbol: "BTC/USDT", timeframe: "15m",
  exchange: "fixture", data_source: "browser-test", connected: true,
  mode: "PAPER", live_mode: false, equity: 10000, initial_equity: 10000,
  quote: "USDT", balance: 10000, unrealized_pnl: 0, realized_pnl: 0,
  realized_pnl_today: 0, daily_loss: 0, drawdown: 0, exposure_ratio: 0,
  pending_orders: 0, positions: [], stats: { win_rate: 0, trades: 0, profit_factor: 0, avg_r: 0, max_drawdown: 0 },
  risk: { risk_per_trade: 0.01, max_daily_loss: 0.03, max_total_exposure: 0.2, max_drawdown: 0.1, max_open_positions: 3 },
  gate: { passed: true, summary: "fixture" }, session: { jadwal: "—", terbuka: true, trade_hari_ini: 0, batas_harian: 10 },
  instrument: {}, analysis: {}, adaptive_risk: {}, broker: { name: "paper", live: null },
  events: [], chart: { open: [], high: [], low: [], close: [], ema_fast: [], ema_slow: [], ts: [] }
};
const fixtures = {
  "/api/state": state,
  "/api/history": { orders: [], trades: [], signals: [] },
  "/api/report": {},
  "/api/briefing": { decision: "tunggu", confidence: 0, situation: "Browser fixture", evidence: [], risks: [] },
  "/api/health": { ok: true, uptime_s: 60, tick_age_s: 2, tick_count: 12, consecutive_errors: 0, db_size_mb: 0.2, disk_free_mb: 5000, heartbeat_age_s: 2, issues: [], last_error: null },
  "/api/research": {
    available: true, samples: 30, required_samples: 30,
    walk_forward: { window_count: 1, test_summary: { avg_r: 0.1, win_rate: 0.55 } },
    monte_carlo: { ending_equity: { median: 10050 }, max_drawdown: { p95: 0.04 }, probability_below_start: 0.3 }
  }
};
await page.route("**/api/**", async route => {
  const path = new URL(route.request().url()).pathname;
  const body = fixtures[path] ?? { ok: true };
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(body) });
});

try {
  await page.goto(base, { waitUntil: "domcontentloaded" });
  await page.waitForFunction(() => document.getElementById("hdr-symbol")?.textContent === "BTC/USDT");
  const navLabels = await page.locator("#main-tabs button").allTextContents();
  if (navLabels.length !== 8) throw new Error(`Expected 8 nav items, found ${navLabels.length}`);

  await page.locator('[data-panel="research"]').click();
  await page.getByText("RESULTS LOADED", { exact: true }).waitFor({ timeout: 5000 });
  if ((await page.locator("#research-wf-avg").textContent()).trim() !== "0.100 R") {
    throw new Error("Research result did not render");
  }

  await page.locator('[data-panel="monitoring"]').click();
  await page.getByText("HEALTHY", { exact: true }).waitFor({ timeout: 5000 });
  if ((await page.locator("#mon-ticks").textContent()).trim() !== "12") {
    throw new Error("Backend health metrics did not render");
  }

  await mkdir("artifacts", { recursive: true });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.screenshot({ path: "artifacts/aegis-desktop.png", fullPage: true });
  const desktop = await page.evaluate(() => ({
    width: innerWidth, documentWidth: document.documentElement.scrollWidth,
    sidebarWidth: Math.round(document.querySelector(".sidebar").getBoundingClientRect().width)
  }));
  if (desktop.documentWidth > desktop.width + 1) throw new Error("Desktop horizontal overflow");

  await page.setViewportSize({ width: 768, height: 1024 });
  await page.waitForTimeout(100);
  const tablet = await page.evaluate(() => ({
    width: innerWidth, documentWidth: document.documentElement.scrollWidth,
    navColumns: getComputedStyle(document.querySelector(".main-tabs")).gridTemplateColumns
  }));
  if (tablet.documentWidth > tablet.width + 1) throw new Error("Tablet horizontal overflow");

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForTimeout(100);
  const mobile = await page.evaluate(() => ({
    width: innerWidth, documentWidth: document.documentElement.scrollWidth,
    navColumns: getComputedStyle(document.querySelector(".main-tabs")).gridTemplateColumns
  }));
  if (mobile.documentWidth > mobile.width + 1) throw new Error("Mobile horizontal overflow");
  await page.screenshot({ path: "artifacts/aegis-mobile.png", fullPage: true });

  if (pageErrors.length) throw new Error("Browser runtime errors: " + pageErrors.join(" | "));
  console.log(JSON.stringify({ ok: true, navItems: navLabels.length, desktop, tablet, mobile, pageErrors }));
} finally {
  await browser.close();
  await new Promise(resolve => server.close(resolve));
}
