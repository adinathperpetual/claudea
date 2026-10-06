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

    @app.get("/finesse/home", response_class=HTMLResponse)
    def home(req: Request):
        if not ok(req):
            return RedirectResponse("/finesse")
        return "<html><body><nav><a href='#'>Masters</a> <a href='/finesse/clients'>Client Master</a></nav></body></html>"

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
