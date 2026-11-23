// Builds the SOP PDF with fresh screenshots of the tool, using SAMPLE data only.
//
//   1. Start a throw-away service with sample clients (never the real data folder):
//        set CNE_DATA_DIR=<empty test folder> and test tokens, then
//        python docs/sop/seed_sample.py  and  python -m finesse_sync serve
//   2. node docs/sop/build_sop.js      ->  docs/sop/img/*.png + docs/sop/Contract-Note-Extractor-SOP.pdf
//
// Optional: CNE_VENDOR=<folder with pdf.min.js, pdf.worker.min.js, xlsx.full.min.js> when the
// PC has no internet; CNE_URL (default http://127.0.0.1:8766/).
const fs = require("fs");
const path = require("path");
let chromium;
try { ({ chromium } = require("playwright")); }
catch { ({ chromium } = require("/opt/node22/lib/node_modules/playwright")); }

const URL = process.env.CNE_URL || "http://127.0.0.1:8766/";
const VENDOR = process.env.CNE_VENDOR || "";
const IMG = path.join(__dirname, "img");
const wait = ms => new Promise(r => setTimeout(r, ms));

// ---- sample trades: invented stocks, ISINs and clients ----
const SAMPLE = {
  ok: { file: "CN_20112026_RAHUL SHARMA.pdf", broker: "Motilal Oswal", client: "RAHUL SHARMA", date: "20 Nov 2026",
        trades: [{ scrip: "ALPHA INFRA PROJECTS LTD", isin: "INE000A01015", side: "Buy", qty: 1500, wap: 412.3517,
                   gross: 412.1006, basis: "After brokerage ✓ verified", conf: "verified", fills: 2 },
                 { scrip: "BETA PHARMA LTD", isin: "INE000B01017", side: "Sell", qty: 300, wap: 1288.4410,
                   gross: 1289.0855, basis: "After brokerage ✓ verified", conf: "verified", fills: 1 }] },
  ok2: { file: "CN_20112026_MEERA PATEL HUF.pdf", broker: "Zerodha", client: "MEERA PATEL HUF", date: "20/11/2026",
         trades: [{ scrip: "GAMMA TEXTILES LTD", isin: "INE000C01019", side: "Buy", qty: 800, wap: 96.4521,
                    gross: 96.4000, basis: "After brokerage ✓ verified", conf: "verified", fills: 1 }] },
  noacct: { file: "CN_20112026_ANIL KUMAR GUPTA.pdf", broker: "IIFL Capital", client: "ANIL KUMAR GUPTA", date: "20 Nov 2026",
            trades: [{ scrip: "DELTA CEMENT LTD", isin: "INE000D01011", side: "Buy", qty: 250, wap: 2210.5532,
                       gross: 2209.4500, basis: "After brokerage ✓ verified", conf: "verified", fills: 1 }] },
  gross: { file: "CN_20112026_SUNITA RAO.pdf", broker: "Groww", client: "SUNITA RAO", date: "20-11-2026",
           trades: [{ scrip: "EPSILON FOODS LTD", isin: "INE000E01018", side: "Buy", qty: 100, wap: 540.25,
                      gross: 540.25, basis: "⚠ GROSS — no brokerage found", conf: "gross", fills: 1 }] },
  lock: { file: "CN_20112026_K VENKATESH.pdf",
          note: "None of the 4 Client Master clients' PANs or saved passwords opens this file — add its password as an exceptional password" },
  parse: { file: "CN_20112026_SCANNED COPY.pdf", note: "No text layer (scanned image?)" },
};
const SAMPLE_TEXT = [
  "CONTRACT NOTE", "Sample Broker Limited", "Client Name : RAHUL SHARMA", "Trade Date : 20 Nov 2026",
  "ISIN Security Name Quantity WAP Brokerage per Share WAP after brokerage Total BUY Value",
  "INE000A01015 ALPHA INFRA PROJECTS LTD 1500 412.1006 0.2511 412.3517 618527.55",
  "INE000B01017 BETA PHARMA LTD 300 1289.0855 0.6445 1288.4410 386532.30",
  "Net amount payable by client ...",
];

(async () => {
  fs.mkdirSync(IMG, { recursive: true });
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 880 }, deviceScaleFactor: 1.5 });
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  if (VENDOR){
    for (const f of ["pdf.worker.min.js", "pdf.min.js", "xlsx.full.min.js"])
      await page.route("**/" + f, r => r.fulfill({ path: path.join(VENDOR, f) }));
    // offline: serve the same fonts from VENDOR/fonts.css + VENDOR/fonts/*.woff2 (if present)
    const css = path.join(VENDOR, "fonts.css");
    if (fs.existsSync(css)){
      const body = fs.readFileSync(css, "utf8").replace(/url\(fonts\//g, "url(https://fonts.local/");
      await ctx.route("**/fonts.googleapis.com/**", r => r.fulfill({ body, contentType: "text/css" }));
      await ctx.route("https://fonts.local/**", r => r.fulfill({ path: path.join(VENDOR, "fonts", r.request().url().split("/").pop()) }));
    } else await page.route("**/fonts.googleapis.com/**", r => r.abort());
  }
  const shot = (name, sel) => sel ? page.locator(sel).first().screenshot({ path: path.join(IMG, name) })
                                  : page.screenshot({ path: path.join(IMG, name) });
  const span = async (name, fromSel, toSel) => {
    const [a, b, sy] = await page.evaluate(([f, t]) => {
      const r = s => document.querySelector(s).getBoundingClientRect(); return [r(f).toJSON(), r(t).toJSON(), scrollY];
    }, [fromSel, toSel]);
    await page.screenshot({ path: path.join(IMG, name), fullPage: true,
      clip: { x: a.x - 6, y: a.y + sy - 6, width: a.width + 12, height: b.bottom - a.top + 12 } });
  };
  const noToast = () => page.evaluate(() => document.getElementById("toast").classList.remove("on"));
  const closeAll = () => page.evaluate(() => document.querySelectorAll(".overlay.on").forEach(o => o.classList.remove("on")));

  // ---- sign in ----
  await page.goto(URL); await wait(1500);
  await shot("01-signin.png");
  await page.fill("#authUser", "admin"); await page.fill("#authPass", "admin"); await page.click("#authGo"); await wait(400);
  await shot("02-new-password.png");
  await page.fill("#authPass", "sample123"); await page.fill("#authPass2", "sample123"); await page.click("#authGo");
  await wait(2500);
  await page.addStyleTag({ content: ".topbar{position:static!important}" });
  await noToast();

  // ---- contract notes added ----
  await page.evaluate(s => {
    state.pdfs = Object.values(s).map((x, i) => ({ id: 900 + i, name: x.file, size: 180000 + i * 73123, bytes: new Uint8Array() }));
    renderPdfChips();
  }, SAMPLE);
  await page.screenshot({ path: path.join(IMG, "03-main.png") });
  await shot("04-contract-notes.png", "section.card >> nth=0");

  // ---- client master (Finesse sync) ----
  await page.click("#cmHead"); await wait(1500);
  await page.evaluate(() => { document.getElementById("fsReveal").click(); }); await wait(800);
  await shot("05-finesse-sync.png", "#fsPanel");
  await page.click("#fsLogsBtn"); await wait(800);
  await shot("06-sync-log.png", "#fsLogs");
  await page.click("#fsLogsBtn"); await wait(300);
  await page.fill("#fsNewCode", "SR1004"); await page.fill("#fsNewPw", "SUNITA#77"); await page.fill("#fsNewNote", "Confirmed by client on call");
  await shot("07-add-exceptional.png", "#fsPanel");
  await page.fill("#fsNewCode", ""); await page.fill("#fsNewPw", ""); await page.fill("#fsNewNote", "");
  await page.click("#cmHead"); await wait(300);

  // ---- output template ----
  await page.click("#tplHead"); await wait(400);
  await shot("08-template.png", "section.card >> nth=2");
  await page.click("#tplHead"); await wait(300);

  // ---- results with sample data incl. errors ----
  await page.evaluate(([s, text]) => {
    const R = (id, x, extra) => ({ id, file: x.file, status: "ok", broker: x.broker, client: x.client, tradeDate: x.date,
                                   trades: x.trades, lines: text, diag: { header: "sample", roles: ["sample"] }, ...extra });
    state.results = [
      R(900, s.ok), R(901, s.ok2), R(902, s.noacct), R(903, s.gross),
      { id: 904, file: s.lock.file, status: "lock", trades: [], lines: [], note: s.lock.note },
      { id: 905, file: s.parse.file, status: "warn", trades: [], lines: [], note: s.parse.note },
    ];
    recomputeAccounts();
  }, [SAMPLE, SAMPLE_TEXT]);
  await noToast();
  await span("09-results.png", ".actionbar", ".tablewrap");

  // ---- pop-ups ----
  await page.evaluate(() => openFailModal(lockedResults())); await wait(450);
  await shot("10-locked.png", "#overlay .modal"); await closeAll(); await wait(300);
  await page.evaluate(() => openAcctModal()); await wait(450);
  await shot("11-missing-account.png", "#acctOverlay .modal");
  await page.fill("#acctList input[type=text]", "D100103");
  await shot("12-missing-account-filled.png", "#acctOverlay .modal");
  await page.click("#acctList .rb"); await wait(400);                       // Save — the real flow
  await shot("12b-missing-account-saved.png", "#acctOverlay .modal"); await closeAll(); await wait(300);
  await page.evaluate(() => openPriceModal()); await wait(450);
  await shot("13-price.png", "#priceOverlay .modal");
  await page.fill("#priceList input[type=text]", "540.52"); await page.click("#priceList .rb"); await wait(400);
  await shot("13b-price-confirmed.png", "#priceOverlay .modal"); await closeAll(); await wait(300);
  await page.evaluate(() => openInspect(900)); await wait(450);
  await shot("14-inspect.png", "#inspectOverlay .modal"); await closeAll(); await wait(300);

  // account and price now resolved through the pop-ups above
  await noToast();
  await span("15-results-ready.png", ".results-head", ".tablewrap");

  // ---- Upload to Finesse: step 1 (choose), step 3 (confirm), result ----
  await page.setViewportSize({ width: 1280, height: 1500 });
  await page.addStyleTag({ content: "#fpOverlay .modal-body{max-height:none!important}" });   // whole window in the picture
  await page.click("#expFinesse"); await wait(1500);
  await shot("16-upload-choose.png", "#fpOverlay .modal");
  await closeAll(); await wait(300);
  const job = (status, extra = {}) => ({ id: 7001, status, data: { selected: ["a1", "a2", "b1", "c1"], accounts: {
    D100101: { client: "RAHUL SHARMA", candidates: [
      { id: "a1", kind: "new", name: "ALPHA INFRA PROJECTS LTD", row: { trade_date: "2026-11-20", side: "PURCHASE", qty: 1500, rate: 412.35, isin: "INE000A01015" } },
      { id: "a2", kind: "new", name: "BETA PHARMA LTD", row: { trade_date: "2026-11-20", side: "SELL", qty: 300, rate: 1288.44, isin: "INE000B01017" } }] },
    D100102: { client: "MEERA PATEL HUF", candidates: [
      { id: "b1", kind: "duplicate", name: "GAMMA TEXTILES LTD", row: { trade_date: "2026-11-20", side: "PURCHASE", qty: 800, rate: 96.45, isin: "INE000C01019" } }] },
    D100103: { client: "ANIL KUMAR GUPTA", candidates: [
      { id: "c1", kind: "unmapped", name: "DELTA CEMENT LTD", row: { trade_date: "2026-11-20", side: "PURCHASE", qty: 250, rate: 2210.55, isin: "INE000D01011" } }] },
  }, ...extra } });
  await page.evaluate(j => fpShowJob(j), job("awaiting_confirmation")); await wait(500);
  await shot("17-upload-confirm.png", "#fpOverlay .modal");
  await page.evaluate(j => { fpStatus = ""; fpRender(j); fpButtons(); }, job("posted", {
    selected: ["a1", "a2"], post_result: { D100101: { posted: 2, selected: 2, message: "Transactions posted successfully" } },
    timings: { upload_total: 48, post_total: 21 } })); await wait(400);
  await shot("18-upload-posted.png", "#fpOverlay .modal"); await closeAll(); await wait(300);

  await page.setViewportSize({ width: 1280, height: 880 });
  // ---- users ----
  await page.click("#usersLink"); await wait(600);
  await shot("19-users.png", "#adminGrid");

  // ---- service not reachable (status line) ----
  await page.evaluate(() => { fsSetStatus("bad", "Client Master service is not reachable at http://127.0.0.1:8766. Using the local master (last saved data)."); fsShowMain(false); });
  await page.click("#cmHead"); await wait(300);
  await shot("20-not-connected.png", "#fsPanel");

  // ---- illustrations (not screenshots of the tool) ----
  const ill = await ctx.newPage();
  for (const [file, out] of [["finesse-search.html", "21-finesse-search.png"], ["finesse-profile.html", "22-finesse-profile.png"],
                             ["setup-console.html", "23-setup-console.png"]]){
    await ill.goto("file://" + path.join(__dirname, "illustrations", file)); await wait(300);
    await ill.locator("#frame").screenshot({ path: path.join(IMG, out) });
  }

  // ---- print ----
  const sop = await ctx.newPage();
  await sop.goto("file://" + path.join(__dirname, "SOP.html")); await wait(1000);
  const out = path.join(__dirname, "Contract-Note-Extractor-SOP.pdf");
  await sop.pdf({ path: out, format: "A4", printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true, headerTemplate: "<span></span>",
    footerTemplate: '<div style="font:8px Arial;color:#888;width:100%;padding:0 14mm;display:flex;justify-content:space-between">' +
      '<span>CNE-SOP-02 · Contract Note Extractor (Finesse) — Standard Operating Procedure · v1.0</span>' +
      '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>' });
  await browser.close();
  if (errors.length) console.log("page errors:", errors);
  console.log("SOP written:", out);
})();
