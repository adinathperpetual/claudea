// Broker regression tests: feeds saved contract-note text layers (exactly what
// "Inspect text" shows) through the tool's own extraction code in a real browser
// and checks the extracted values.
//
//   1. ./desktop/build.sh        (once — fetches pdf.js / SheetJS into desktop/web/vendor)
//   2. node tests/run.js
//
// To remember a new broker layout: save its "Inspect text" (or a faithful copy)
// in tests/fixtures/ and add an entry to CASES with the values printed on the note.
const fs = require("fs");
const path = require("path");
let chromium;
try { ({ chromium } = require("playwright")); }
catch { ({ chromium } = require("/opt/node22/lib/node_modules/playwright")); }

const ROOT = path.resolve(__dirname, "..");
const PAGE = path.join(ROOT, "desktop", "web", "index.html");

const CASES = [
  {
    name: "Motilal Oswal — Network People (BSE 100 + NSE 442, same ISIN)",
    fixture: "motilal-network-people.txt",
    client: "SAMPLE CLIENT NAME HUF", date: "13 Oct 2026",
    trades: [{ isin: "INE0FFK01017", scrip: "Network People Services Techno", side: "Buy",
               qty: 542, wap: 1841.1930, gross: 1840.2302 }],
  },
  {
    name: "Motilal Oswal — Aeroflex (2000 + 2000, same ISIN)",
    fixture: "motilal-aeroflex.txt",
    client: "SAMPLE CLIENT NAME HUF", date: "19 Nov 2026",
    trades: [{ isin: "INE024001021", scrip: "Aeroflex Industries Limited", side: "Buy",
               qty: 4000, wap: 490.6277, gross: 490.3825 }],
  },
];

(async () => {
  if (!fs.existsSync(PAGE)) { console.error("Run ./desktop/build.sh first (needs " + PAGE + ")"); process.exit(2); }
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.goto("file://" + PAGE);
  await page.waitForFunction(() => typeof extractTrades === "function");

  let failed = 0;
  for (const c of CASES){
    const dump = fs.readFileSync(path.join(__dirname, "fixtures", c.fixture), "utf8").split("\n").filter(s => s.trim());
    const got = await page.evaluate(dump => {
      // rebuild text lines (one item per word) the way pageLines() would
      const lines = dump.map((t, i) => {
        let x = 40;
        const parts = t.split(" ").filter(Boolean).map(w => {
          const it = { str: w, x, y: 800 - i*10, w: w.length*6, h: 8 }; x += (w.length + 1)*6; return it; });
        return { y: 800 - i*10, parts, text: parts.map(p => p.str).join(" "), cells: buildCells(parts) };
      });
      const r = extractTrades(lines);
      return { trades: r.trades, client: findClientName(lines), date: findTradeDate(lines) };
    }, dump);

    const problems = [];
    if (got.client !== c.client) problems.push(`client "${got.client}" ≠ "${c.client}"`);
    if (got.date !== c.date) problems.push(`trade date "${got.date}" ≠ "${c.date}"`);
    if (got.trades.length !== c.trades.length) problems.push(`${got.trades.length} trades ≠ ${c.trades.length}`);
    c.trades.forEach((want, i) => {
      const t = got.trades[i] || {};
      for (const k of ["isin", "scrip", "side", "qty"]) if (t[k] !== want[k]) problems.push(`${k} "${t[k]}" ≠ "${want[k]}"`);
      for (const k of ["wap", "gross"]) if (!(Math.abs(t[k] - want[k]) < 0.00011)) problems.push(`${k} ${t[k]} ≠ ${want[k]}`);
    });
    if (problems.length){ failed++; console.log("✗ " + c.name + "\n    " + problems.join("\n    ")); }
    else console.log("✓ " + c.name + "  →  " + got.trades.map(t => `${t.side} ${t.qty} ${t.scrip} @ ₹${t.wap}`).join("; ")
                     + `  | ${got.client} | ${got.date}`);
  }
  if (errors.length){ failed++; console.log("✗ page errors: " + errors.join("; ")); }
  await browser.close();
  console.log(failed ? `\n${failed} FAILED` : `\nAll ${CASES.length} broker tests passed`);
  process.exit(failed ? 1 : 0);
})();
