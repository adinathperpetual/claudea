// Builds the SOP: takes fresh screenshots of the tool (with SAMPLE data only)
// into docs/img/ and prints docs/SOP.html to docs/Contract-Note-Extractor-SOP.pdf.
//   1. ./desktop/build.sh      (once — puts the offline libraries in desktop/web)
//   2. node docs/build-sop.js
const fs = require("fs");
const os = require("os");
const path = require("path");
let chromium;
try { ({ chromium } = require("playwright")); }
catch { ({ chromium } = require("/opt/node22/lib/node_modules/playwright")); }

const ROOT = path.resolve(__dirname, "..");
const PAGE = "file://" + path.join(ROOT, "desktop", "web", "index.html");
const IMG = path.join(__dirname, "img");
const wait = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  fs.mkdirSync(IMG, { recursive: true });
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "cne-sop-"));
  const csv = path.join(tmp, "client-master.csv");
  fs.writeFileSync(csv, [
    "Client Name,Trading Account,Password",
    "SAMPLE CLIENT NAME HUF,ABC1234,SAMPLE-PW1",
    "Priya Sharma,XYZ5678,SAMPLE-PW2",
    "Amit Verma,PQR4321,",
    "Rohan Mehta,,SAMPLE-PW3",
  ].join("\n"));

  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 860 }, deviceScaleFactor: 1.5 });
  const page = await ctx.newPage();
  const shot = async (name, sel) => {
    const file = path.join(IMG, name);
    if (!sel) return page.screenshot({ path: file });
    return page.locator(sel).first().screenshot({ path: file });
  };
  // screenshot a vertical span of the page (page coordinates, any scroll position)
  const span = async (name, fromSel, toSel) => {
    const [a, b, sy] = await page.evaluate(([f, t]) => {
      const r = s => document.querySelector(s).getBoundingClientRect();
      return [r(f).toJSON(), r(t).toJSON(), scrollY];
    }, [fromSel, toSel]);
    return page.screenshot({ path: path.join(IMG, name), fullPage: true,
      clip: { x: a.x - 6, y: a.y + sy - 6, width: a.width + 12, height: b.bottom - a.top + 12 } });
  };

  await page.goto(PAGE); await wait(900);
  await shot("01-signin.png");
  await page.fill("#authUser", "admin"); await page.fill("#authPass", "admin"); await page.click("#authGo"); await wait(300);
  await shot("02-new-password.png");
  await page.fill("#authPass", "sample123"); await page.fill("#authPass2", "sample123"); await page.click("#authGo"); await wait(500);
  await page.addStyleTag({ content: ".topbar{position:static!important}" });   // no sticky bar over screenshots

  await page.setInputFiles("#clientInput", csv); await wait(400);
  await page.evaluate(() => { const t = document.getElementById("toast"); t.classList.remove("on"); });
  await page.evaluate(() => {
    state.pdfs = [
      { id: 901, name: "CN_19112026_SAMPLE CLIENT NAME HUF.pdf", size: 412345, bytes: new Uint8Array() },
      { id: 902, name: "CN_13102026_SAMPLE CLIENT NAME HUF.pdf", size: 523456, bytes: new Uint8Array() },
      { id: 903, name: "Contract_Note_20112026_Priya Sharma.pdf", size: 198765, bytes: new Uint8Array() },
    ];
    renderPdfChips();
  });
  await page.screenshot({ path: path.join(IMG, "03-main.png") });
  await shot("04-contract-notes.png", "section.card >> nth=0");
  await page.click(".reveal-btn"); await wait(200);
  await shot("05-client-master.png", "#dataCardsGrid");
  await shot("06-template.png", "section.card >> nth=2");

  // the "Complete the client master" check
  await page.click("#dlClients"); await wait(500);
  await shot("07-master-gap.png", "#gapOverlay .modal");
  await page.click("#closeGap"); await wait(300);

  // sample results (illustration only — values from two Motilal Oswal notes)
  const lines = fs.readFileSync(path.join(ROOT, "tests", "fixtures", "motilal-aeroflex.txt"), "utf8").split("\n").slice(0, 60);
  await page.evaluate(lines => {
    state.results = [
      { id: 901, file: "CN_19112026_SAMPLE CLIENT NAME HUF.pdf", status: "ok", broker: "Motilal Oswal",
        client: "SAMPLE CLIENT NAME HUF", tradeDate: "19 Nov 2026", lines,
        diag: { header: "Motilal Oswal EQUITY SEGMENT summary", roles: ["Motilal transposed summary (one field per line)"] },
        trades: [{ scrip: "Aeroflex Industries Limited", isin: "INE024001021", side: "Buy", qty: 4000, wap: 490.6277,
                   gross: 490.3825, basis: "After brokerage ✓ verified", conf: "verified", fills: 2 }] },
      { id: 902, file: "CN_13102026_SAMPLE CLIENT NAME HUF.pdf", status: "ok", broker: "Motilal Oswal",
        client: "SAMPLE CLIENT NAME HUF", tradeDate: "13 Oct 2026", lines: [],
        trades: [{ scrip: "Network People Services Techno", isin: "INE0FFK01017", side: "Buy", qty: 542, wap: 1841.193,
                   gross: 1840.2302, basis: "After brokerage ✓ verified", conf: "verified", fills: 2 }] },
      { id: 904, file: "CN_21112026_Anita Desai.pdf", status: "ok", broker: "Zerodha",
        client: "ANITA DESAI", tradeDate: "21/11/2026", lines: [],
        trades: [{ scrip: "TATA STEEL LTD", isin: "INE081A01020", side: "Sell", qty: 150, wap: 151.2034,
                   gross: 151.35, basis: "After brokerage (computed)", conf: "computed", fills: 1 }] },
      { id: 903, file: "Contract_Note_20112026_Priya Sharma.pdf", status: "lock", trades: [], lines: [] },
    ];
    recomputeAccounts();
    document.getElementById("toast").classList.remove("on");
  }, lines);
  await span("08-results.png", ".actionbar", ".tablewrap");

  await page.evaluate(() => openFailModal(lockedResults())); await wait(450);
  await shot("09-locked.png", "#overlay .modal");
  await page.evaluate(() => document.getElementById("overlay").classList.remove("on")); await wait(300);
  await page.evaluate(() => openAcctModal()); await wait(450);
  await shot("10-missing-account.png", "#acctOverlay .modal");
  await page.evaluate(() => document.getElementById("acctOverlay").classList.remove("on")); await wait(300);
  await page.evaluate(() => openInspect(901)); await wait(450);
  await shot("11-inspect.png", "#inspectOverlay .modal");
  await page.evaluate(() => document.getElementById("inspectOverlay").classList.remove("on")); await wait(300);
  await shot("12-users.png", "#adminGrid");

  // print the SOP
  const sop = await ctx.newPage();
  await sop.goto("file://" + path.join(__dirname, "SOP.html")); await wait(800);
  const out = path.join(__dirname, "Contract-Note-Extractor-SOP.pdf");
  await sop.pdf({ path: out, format: "A4", printBackground: true, preferCSSPageSize: true,
    displayHeaderFooter: true, headerTemplate: "<span></span>",
    footerTemplate: '<div style="font:8px Arial;color:#888;width:100%;padding:0 14mm;display:flex;justify-content:space-between">' +
      '<span>CNE-SOP-01 · Contract Note Extractor — Standard Operating Procedure · v1.0</span>' +
      '<span>Page <span class="pageNumber"></span> of <span class="totalPages"></span></span></div>' });
  await browser.close();
  fs.rmSync(tmp, { recursive: true, force: true });
  console.log("SOP written:", out);
})();
