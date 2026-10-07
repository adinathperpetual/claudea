"""A tiny stand-in for the Finesse portal used by the tests: login form
(user id + password + PAN), a paginated client table, a JSON API and a CSV export."""
from __future__ import annotations

import secrets
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse

USER, PASSWORD, PAN = "u123", "s3cret!", "AAAPZ1234C"

CLIENTS = [
    {"Client Code": f"T{i:04d}", "Client Name": n, "PAN No": p}
    for i, (n, p) in enumerate([
        ("GAYATRI JAIN", "ABCPJ1234K"), ("SOHAIL FAKHRUDDIN PATEL", "BCDPP2345L"),
        ("MAHAVIR JALAMCHAND OSWAL HUF", "CDEHO3456M"), ("RAVI KUMAR", "DEFPK4567N"),
        ("ANITA DESAI", "EFGPD5678P"), ("BAD PAN CLIENT", "12345"), ("KIRAN SHAH", "GHIPS7890R"),
    ], 1)
]


# Synthetic clients shaped like the real Finesse Angular Material grid:
# (client code, name, trading account shown in brackets or "", badge, PAN)
MAT_CLIENTS = [
    ("PCA00001", "Alpha Imports Pvt Ltd", "D000001", "", "AAACA0001A"),
    ("PCA00002", "Beta Technologies", "3000002", "", "AAAFB0002B"),
    ("PCA00003", "Chetan Rao", "3000003", "", "AAAPR0003C"),
    ("PCA00004", "Deepa Kulkarni", "DK7004", "", "AAAPK0004D"),
    ("PCA00005", "Esha Verma", "", "Joint", "AAAPV0005E"),
    ("PCA00006", "Farhan Shaikh", "", "", "AAAPS0006F"),
    ("PCA00007", "Gaurav Mehta", "", "Joint", "AAAPM0007G"),
    ("PCA00008", "Harsh Jain", "4000008", "", "AAAPJ0008H"),
    ("PCA00009", "Ishaan Kumar Jain HUF", "", "Proprietorship", "AAAHJ0009J"),
    ("PCA00010", "Jaya Porwal", "3000010", "", "AAAPP0010K"),
    ("PCA00011", "Kunal Joshi", "KJ26011", "", "AAAPJ0011L"),
    ("PCA00012", "Lata Iyer", "6000012", "", "AAAPI0012M"),
    ("PCA00013", "Manoj Nair", "D000013", "", "AAAPN0013N"),
]

MAT_PAGE = """<html><body><div class="app"><h1>Clients</h1>
<table mat-table class="mat-mdc-table"><thead><tr mat-header-row class="mat-mdc-header-row">
<th class="mat-mdc-header-cell cdk-column-clientCode">Code</th><th class="mat-mdc-header-cell cdk-column-clientName">Client</th>
<th class="mat-mdc-header-cell cdk-column-familyName">Family</th><th class="mat-mdc-header-cell cdk-column-clientPan">PAN</th>
<th class="mat-mdc-header-cell cdk-column-actions"></th></tr></thead><tbody role="rowgroup" id="rows"></tbody></table>
<div class="mat-mdc-paginator"><div class="mat-mdc-paginator-page-size-select" id="sizeSel" style="cursor:pointer">5</div>
<div id="overlay"></div>
<button class="mat-mdc-paginator-navigation-next" aria-label="Next page" id="next">&gt;</button></div></div>
<script>
const data = __DATA__; let size = 5, page = 0;
function render(){
  const rows = data.slice(page*size, page*size+size).map(([code,name,acct,badge,pan]) =>
    `<tr role="row" mat-row class="mat-mdc-row"><td class="mat-mdc-cell cdk-column-clientCode"><div class="flex"><div><span class="dot-green"></span></div><div class="ml-2"> ${code} </div></div></td>`+
    `<td class="mat-mdc-cell cdk-column-clientName"><a title="Click to go to Profile View" href="#/profile/x"> ${name} ${acct?`<span>(${acct})</span>`:''}<!----></a>${badge?`<span class="badge"> ${badge} </span>`:''}</td>`+
    `<td class="mat-mdc-cell cdk-column-familyName"> ${name} - Family </td><td class="mat-mdc-cell cdk-column-clientPan"> ${pan} </td>`+
    `<td class="mat-mdc-cell cdk-column-actions"><button><span>visibility</span></button></td></tr>`).join('');
  // the real grid redraws a moment after the click (no page load)
  setTimeout(() => { document.getElementById('rows').innerHTML = rows;
    const last = (page+1)*size >= data.length; const n = document.getElementById('next');
    n.disabled = last; n.classList.toggle('mat-mdc-button-disabled', last); }, 400);
}
document.getElementById('next').onclick = () => { page++; render(); };
document.getElementById('sizeSel').onclick = () => {
  document.getElementById('overlay').innerHTML = [5,10].map(n => `<mat-option class="mat-mdc-option" data-n="${n}">${n}</mat-option>`).join('');
  document.querySelectorAll('mat-option').forEach(o => o.onclick = () => { size = +o.dataset.n; page = 0;
    document.getElementById('sizeSel').textContent = size; document.getElementById('overlay').innerHTML = ''; render(); });
};
setTimeout(render, 600);   // async first draw, like Angular
</script></body></html>"""


# Angular-style login: landing page -> (late) form with an unlabelled id box + password
# -> second screen asking for PAN (worded without "PAN") -> JSON login call.
SPA_LOGIN = """<html><body><div id="app"></div><script>
const app = document.getElementById('app'); let uid = '', pwd = '';
const field = (label, id, type) => `<mat-form-field class="mat-mdc-form-field"><mat-label>${label}</mat-label>`+
  `<input id="${id}" type="${type}" class="mat-mdc-input-element"></mat-form-field>`;
function landing(){ app.innerHTML = '<h1>Welcome to Finesse</h1><button id="go">Login</button>';
  document.getElementById('go').onclick = () => setTimeout(step1, 700); }
function step1(){ app.innerHTML = field('Your code', 'mat-input-0', 'text') + field('Secret', 'mat-input-1', 'password') +
  '<button type="button" id="next">Next</button>';
  document.getElementById('next').onclick = () => { uid = document.getElementById('mat-input-0').value;
    pwd = document.getElementById('mat-input-1').value; setTimeout(step2, 500); }; }
function step2(){ app.innerHTML = field('Permanent Account No.', 'mat-input-2', 'text') +
  '<button type="button" id="verify">Verify</button><div class="error" id="err"></div>';
  document.getElementById('verify').onclick = async () => {
    const r = await fetch('/finesse/api/login', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({userId: uid, password: pwd, pan: document.getElementById('mat-input-2').value})});
    if (r.ok) location.href = '/finesse/home'; else document.getElementById('err').textContent = 'Invalid credentials'; }; }
setTimeout(landing, 1000);
</script></body></html>"""


# Shell like Finesse's index.html (Fuse template): <base href="./">, a splash screen, and
# the sign-in form drawn later by main.js. Opened without the trailing "/", main.js is
# looked up one folder too high and the page stays blank.
FUSE_SHELL = """<!doctype html><html><head><base href="./"></head><body>
<fuse-splash-screen style="position:fixed;inset:0;pointer-events:none">Loading</fuse-splash-screen>
<app-root></app-root><script src="main.js" type="module"></script></body></html>"""

FUSE_MAIN = """
const root = document.querySelector('app-root');
setTimeout(() => {
  root.innerHTML = `<form id="f"><mat-form-field><mat-label>User ID</mat-label><input id="userId" formcontrolname="userId" matinput></mat-form-field>
    <mat-form-field><mat-label>Password</mat-label><input id="password" type="password" formcontrolname="password"></mat-form-field>
    <mat-form-field><mat-label>PAN</mat-label><input id="pan" formcontrolname="pan"></mat-form-field>
    <label><input type="checkbox"> Remember me</label><button type="submit">Sign in</button><div class="error" id="err"></div></form>`;
  document.body.classList.add('fuse-splash-screen-hidden');
  document.querySelector('fuse-splash-screen').remove();
  document.getElementById('f').onsubmit = async (e) => { e.preventDefault();
    const r = await fetch('api/login', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({userId: userId.value, password: password.value, pan: pan.value})});
    if (r.ok) { location.hash = '#/dashboard'; root.innerHTML = '<h1>Dashboard</h1>'; }
    else document.getElementById('err').textContent = 'Wrong user id or password'; };
}, 1500);
"""


# "Client Master Report" as Finesse offers it: Reports > Corporate Reports > (left panel)
# Other Reports > dropdown > Generate -> an Excel download with title rows and extra columns.
REPORT_CLIENTS = [
    # code, name, family, pan, joint holder pan, trading account, bank account
    ("PCA00101", "Rohan Desai", "Rohan Desai - Family", "AAAPD0101A", "AAAPD0909Z", "D100101", "50100011122233"),
    ("PCA00102", "Sneha Kulkarni", "Kulkarni - Family", "AAAPK0102B", "", "3100102", "50100011122234"),
    ("PCA00103", "Tejas Shah HUF", "Shah - Family", "AAAHS0103C", "", "", "50100011122235"),
    ("PCA00104", "Usha Rao", "Rao - Family", "AAAPR0104D", "", "UR4104", "50100011122236"),
]

CORP_REPORTS = """<html><body><h1>Corporate Reports</h1>
<aside><mat-expansion-panel-header id="other" style="cursor:pointer">Other Reports</mat-expansion-panel-header>
<div id="panel"></div></aside><main id="main"></main><div id="overlay"></div>
<script>
document.getElementById('other').onclick = () => {
  document.getElementById('panel').innerHTML = '<mat-select id="sel" role="combobox" style="cursor:pointer">Select report</mat-select>';
  document.getElementById('sel').onclick = () => {
    document.getElementById('overlay').innerHTML = ['Client Ledger', 'Client Master Report', 'Holding Report']
      .map(n => `<mat-option role="option" style="cursor:pointer">${n}</mat-option>`).join('');
    document.querySelectorAll('mat-option').forEach(o => o.onclick = () => {
      document.getElementById('overlay').innerHTML = '';
      document.getElementById('sel').textContent = o.textContent;
      document.getElementById('main').innerHTML = `<h2>${o.textContent}</h2><button id="gen">Generate</button>`;
      document.getElementById('gen').onclick = () => setTimeout(() => {
        const a = document.createElement('a'); a.href = '/finesse/report/client-master'; a.download = 'ClientMasterReport.xlsx';
        document.body.append(a); a.click(); }, 1500);
    });
  };
};
</script></body></html>"""


def report_xlsx() -> bytes:
    import io
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(["Perpetual Investments — Client Master Report"])
    ws.append(["Generated on 07-10-2026"])
    ws.append([])
    ws.append(["Sr No", "Client Code", "Client Name", "Family Name", "PAN", "Joint Holder PAN",
               "Trading Account No", "Bank Account No", "RM Name"])
    for i, (code, name, fam, pan, jpan, acct, bank) in enumerate(REPORT_CLIENTS, 1):
        ws.append([i, code, name, fam, pan, jpan, acct, bank, "Nikhil"])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def make_app(state: dict) -> FastAPI:
    app = FastAPI()
    sessions: set[str] = set()
    state.setdefault("logins", 0)

    def ok(req: Request) -> bool:
        return req.cookies.get("FSID") in sessions

    @app.get("/finesse", response_class=HTMLResponse)
    def login_page(req: Request):
        if ok(req):
            return RedirectResponse("/finesse/home")
        return """<html><body><form method=post action=/finesse/login>
          <label for=uid>User ID</label><input id=uid name=userId type=text>
          <input name=pwd type=password placeholder=Password>
          <input name=panNo type=text placeholder="PAN">
          <div class="error">""" + state.get("err", "") + """</div>
          <button type=submit>Login</button></form></body></html>"""

    @app.post("/finesse/login")
    def login(userId: str = Form(""), pwd: str = Form(""), panNo: str = Form("")):
        if (userId, pwd, panNo.upper()) != (USER, PASSWORD, PAN):
            state["err"] = "Invalid credentials"
            return RedirectResponse("/finesse", status_code=303)
        state["err"] = ""
        state["logins"] += 1
        sid = secrets.token_hex(8)
        sessions.add(sid)
        r = RedirectResponse("/finesse/home", status_code=303)
        r.set_cookie("FSID", sid)
        return r

    @app.post("/finesse/api/login")
    async def api_login(req: Request):
        b = await req.json()
        if (b.get("userId"), b.get("password"), str(b.get("pan", "")).upper()) != (USER, PASSWORD, PAN):
            return JSONResponse({"success": False, "message": "Invalid"}, status_code=401)
        state["logins"] += 1
        sid = secrets.token_hex(8)
        sessions.add(sid)
        r = JSONResponse({"success": True})
        r.set_cookie("FSID", sid)
        return r

    @app.get("/finesse/spa", response_class=HTMLResponse)
    def spa_login():
        return SPA_LOGIN

    @app.get("/finesse/fuse", response_class=HTMLResponse)   # no redirect to "/fuse/"
    def fuse_no_slash():
        return FUSE_SHELL

    @app.get("/finesse/fuse/", response_class=HTMLResponse)
    def fuse_shell():
        return FUSE_SHELL

    @app.get("/finesse/fuse/main.js")
    def fuse_main():
        return PlainTextResponse(FUSE_MAIN, media_type="text/javascript")

    @app.post("/finesse/fuse/api/login")
    async def fuse_login(req: Request):
        return await api_login(req)

    @app.get("/finesse/corporate-reports", response_class=HTMLResponse)
    def corporate_reports(req: Request):
        if not ok(req):
            return RedirectResponse("/finesse")
        return CORP_REPORTS

    @app.get("/finesse/report/client-master")
    def report_download(req: Request):
        from fastapi.responses import Response
        if not ok(req):
            return RedirectResponse("/finesse")
        state["reports"] = state.get("reports", 0) + 1
        return Response(report_xlsx(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": 'attachment; filename="ClientMasterReport.xlsx"'})

    @app.get("/finesse/blank", response_class=HTMLResponse)
    def blank():
        return "<html><body><p>Maintenance</p></body></html>"

    @app.get("/finesse/home", response_class=HTMLResponse)
    def home(req: Request):
        if not ok(req):
            return RedirectResponse("/finesse")
        return ("<html><body><nav><a href='#'>Masters</a> <a href='/finesse/clients'>Client Master</a></nav>"
                "<header><span id='rep' style='cursor:pointer'>Reports</span><div id='repmenu'></div></header>"
                "<script>document.getElementById('rep').onclick = () => { document.getElementById('repmenu').innerHTML ="
                " \"<a href='/finesse/corporate-reports'>Corporate Reports</a> <a href='#'>Client Reports</a>\"; };</script>"
                "</body></html>")

    @app.get("/finesse/clients", response_class=HTMLResponse)
    def clients(req: Request, page: int = 1):
        if not ok(req):
            return RedirectResponse("/finesse")
        per = 3
        rows = state.get("clients", CLIENTS)
        chunk = rows[(page - 1) * per: page * per]
        trs = "".join(f"<tr><td>{c['Client Code']}</td><td>{c['Client Name']}</td><td>{c['PAN No']}</td></tr>" for c in chunk)
        last = page * per >= len(rows)
        nxt = "<button disabled>Next</button>" if last else f"<a href='/finesse/clients?page={page + 1}'>Next</a>"
        return f"""<html><body><table><thead><tr><th>Client Code</th><th>Client Name</th><th>PAN No</th></tr></thead>
          <tbody>{trs}</tbody></table>{nxt}</body></html>"""

    @app.get("/finesse/clients-material", response_class=HTMLResponse)
    def clients_material(req: Request):
        if not ok(req):
            return RedirectResponse("/finesse")
        import json
        return MAT_PAGE.replace("__DATA__", json.dumps(MAT_CLIENTS))

    @app.get("/finesse/api/clients")
    def api_clients(req: Request, page: int = 1, pageSize: int = 500):
        if not ok(req):
            return JSONResponse({"error": "unauthorised"}, status_code=401)
        rows = state.get("clients", CLIENTS)
        return {"data": {"items": rows[(page - 1) * pageSize: page * pageSize], "total": len(rows)}}

    @app.get("/finesse/export/clients.csv")
    def export(req: Request):
        if not ok(req):
            return RedirectResponse("/finesse")
        rows = state.get("clients", CLIENTS)
        body = "Client Master Report\n\nClient Code,Client Name,PAN No\n" + "".join(
            f"{c['Client Code']},{c['Client Name']},{c['PAN No']}\n" for c in rows)
        return PlainTextResponse(body, media_type="text/csv")

    @app.post("/finesse/_expire")
    def expire():
        sessions.clear()
        return {"ok": True}

    return app


class FakeFinesse:
    def __init__(self):
        self.state: dict = {}
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            self.port = s.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/finesse"
        cfg = uvicorn.Config(make_app(self.state), host="127.0.0.1", port=self.port, log_level="warning")
        self.server = uvicorn.Server(cfg)
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self):
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                break
            time.sleep(0.05)
        return self

    def __exit__(self, *exc):
        self.server.should_exit = True
        self.thread.join(5)
