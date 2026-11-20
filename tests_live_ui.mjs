import { createServer } from "node:http";
import { readFile } from "node:fs/promises";
import { chromium } from "playwright";

const html = await readFile("static/live.html", "utf8");
const token = "live-ui-test-token";
const now = Math.floor(Date.now() / 1000);
const state = {
  ts: now, uptime_s: 7200, symbol: "BTC/USDT", timeframe: "15m",
  exchange: "fixture-exchange", data_source: "browser-fixture", connected: true,
  mode: "PAPER", live_mode: false, equity: 10020, initial_equity: 10000,
  quote: "USDT", balance: 9950, unrealized_pnl: 20, realized_pnl: 0,
  daily_loss: 0, drawdown: 0.002, exposure_ratio: 0.05,
  positions: [{symbol:"BTC/USDT",side:"LONG",size:0.01,entry:50000,stop:49000,take_profit:52000,pnl:20}],
  risk: {risk_per_trade:0.005,max_daily_loss:0.03,max_total_exposure:0.6,max_drawdown:0.1,max_open_positions:3},
  gate: {passed:true,summary:"All data checks passed"},
  analysis: {htf_trend:"up",divergence:"none",pattern:"bullish_engulfing",atr_pct_rank:0.42,squeeze:false},
  adaptive_risk: {multiplier:0.9},
  events: [{ts:now,level:"INFO",event:"tick",message:"Fixture tick processed"}],
  chart: {close:[50000,50100,50200,50150,50300],ema_fast:[50020,50050,50110,50130,50180],ema_slow:[49900,49950,50000,50040,50080],ts:[1,2,3,4,5]}
};
const fixtures = {
  "/api/state": state,
  "/api/history": {
    orders:[{ts:now,symbol:"BTC/USDT",side:"BUY",type:"LIMIT",qty:0.01,price:50000,status:"OPEN"}],
    trades:[{closed_at:now,symbol:"BTC/USDT",side:"LONG",pnl:28,r_multiple:1.2,close_reason:"take_profit"}],
    signals:[{ts:now,symbol:"BTC/USDT",side:"LONG",score:0.82,action:"paper_entry"}]
  },
  "/api/briefing": {decision:"tunggu",confidence:0.66,situation:"BTC/USDT market fixture",evidence:["HTF up"],risks:["spread wide"],actions:["wait for next closed bar"]},
  "/api/portfolio": {quote:"USDT",total_value:10020,cash:9950,invested:70,pnl:20,concentration:0.98,holdings:[{symbol:"BTC/USDT",qty:0.0014,value:70,weight:0.007,pnl:20}]},
  "/api/research": {available:true,samples:30,walk_forward:{window_count:3,test_summary:{avg_r:0.12,win_rate:0.56}},monte_carlo:{max_drawdown:{p95:0.08},probability_below_start:0.21},assumptions:{source:"fixture",risk_fraction_note:"illustrative only"}},
  "/api/live/status": {
    mode:"PAPER",exchange:"binance",live_requested:false,adapter:"paper",
    adapter_present:false,adapter_connected:false,dry_run:true,orders_sent:0,
    orders_rejected:0,last_reconcile_ts:0,reconciled_orders:0,
    allowlisted_symbols:[],market_orders_allowed:false,max_order_notional:100,
    execution_state:"LOCKED",execution_enabled:false,read_only:true,
    lock_reason:"LIVE remains locked in this V3 test fixture."
  },
  "/api/health": {ok:true,uptime_s:7200,tick_age_s:2,tick_count:120,consecutive_errors:0,issues:[]},
  "/api/report": {metrics:{trades:12,win_rate:0.58,profit_factor:1.4,max_drawdown:0.04,net_pnl:28,expectancy_r:0.12}}
};
const server = createServer((req, res) => {
  if (req.url === "/live" || req.url.startsWith("/live?")) {
    res.writeHead(200, {"content-type":"text/html; charset=utf-8","cache-control":"no-store"});
    res.end(html.replace("__AEGIS_CSRF__", token));
    return;
  }
  res.writeHead(404); res.end("not found");
});
await new Promise(resolve => server.listen(0,"127.0.0.1",resolve));
const {port} = server.address();
const browser = await chromium.launch({headless:true,args:["--no-sandbox"]});
const page = await browser.newPage({viewport:{width:1440,height:1000}});
const pageErrors = [];
let csrfValid = true;
page.on("pageerror",error=>pageErrors.push(error.message));
await page.route("**/api/**",async route=>{
  if (route.request().headers()["x-aegis-csrf"] !== token) csrfValid = false;
  const path = new URL(route.request().url()).pathname;
  const body = fixtures[path];
  if (!body) {
    await route.fulfill({status:404,contentType:"application/json",body:JSON.stringify({detail:"not found"})});
    return;
  }
  await route.fulfill({status:200,contentType:"application/json",body:JSON.stringify(body)});
});

try {
  await page.goto("http://127.0.0.1:" + port + "/live",{waitUntil:"domcontentloaded"});
  await page.waitForFunction(()=>document.querySelector("#k-mode")?.textContent==="PAPER" &&
    document.querySelector("#position-count")?.textContent==="1 positions" &&
    document.querySelector("#report-trades")?.textContent==="12");
  const locked = (await page.locator("#execution-pill").textContent()).trim();
  if (!locked.includes("LOCKED")) throw new Error("LIVE execution badge is not locked");
  if ((await page.locator("#adapter-dryrun").textContent()).trim() !== "NOT ACTIVE") {
    throw new Error("Paper runtime incorrectly reports an active LIVE dry-run adapter");
  }
  if ((await page.locator("#positions tbody tr").count()) !== 1) throw new Error("Position row not rendered");
  if ((await page.locator("#orders tbody tr").count()) !== 1) throw new Error("Order row not rendered");
  if ((await page.locator("#events .event").count()) !== 1) throw new Error("Audit event not rendered");
  if ((await page.locator("#portfolio-holdings tbody tr").count()) !== 1) throw new Error("Portfolio holding not rendered");
  if ((await page.locator("#closed-trades tbody tr").count()) !== 1) throw new Error("Closed trade history not rendered");
  if ((await page.locator("#signals tbody tr").count()) !== 1) throw new Error("Signal history not rendered");
  if ((await page.locator("#briefing-decision").textContent()).trim() !== "tunggu") throw new Error("Briefing not rendered");
  if ((await page.locator("#research-state").textContent()).trim() !== "RESULTS AVAILABLE") throw new Error("Research data not rendered");
  if (!csrfValid) throw new Error("One or more API requests omitted the CSRF header");

  const viewports = [];
  for (const width of [1440,768,390]) {
    await page.setViewportSize({width,height:width===390?844:1000});
    await page.waitForTimeout(80);
    const metrics = await page.evaluate(()=>({
      width:innerWidth,
      documentWidth:document.documentElement.scrollWidth,
      canvasWidth:document.querySelector("#price-chart").getBoundingClientRect().width
    }));
    if (metrics.documentWidth > metrics.width + 1) throw new Error("Horizontal overflow at " + width + "px");
    if (metrics.canvasWidth <= 0) throw new Error("Chart collapsed at " + width + "px");
    viewports.push(metrics);
  }
  if (pageErrors.length) throw new Error("Browser runtime errors: "+pageErrors.join(" | "));
  console.log(JSON.stringify({ok:true,locked,csrfValid,viewports,pageErrors}));
} finally {
  await browser.close();
  await new Promise(resolve=>server.close(resolve));
}
