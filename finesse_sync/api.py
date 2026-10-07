"""HTTP API (FastAPI) + scheduler. Also serves the extractor (index.html) at "/".

Auth: every /api route (except /api/health) needs ``Authorization: Bearer <token>``.
  * admin tokens (CNE_ADMIN_TOKENS)         — Client Master screen, sync, edits
  * extractor tokens (CNE_EXTRACTOR_TOKENS) — password lookup + name/code directory only
"""
from __future__ import annotations

import hmac
import io
import json
import logging
import threading
from contextlib import asynccontextmanager
from typing import Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import db, master, sync
from .config import ROOT, ConfigError, get_settings
from .records import read_delimited
from .security import Cipher

log = logging.getLogger("finesse_sync.api")

Role = Literal["admin", "extractor"]


def _match(token: str, allowed: list[str]) -> bool:
    return any(hmac.compare_digest(token, a) for a in allowed)


def auth(authorization: str = Header(default="")) -> Role:
    s = get_settings()
    token = authorization[7:].strip() if authorization.lower().startswith("bearer ") else ""
    if token and _match(token, s.admin_tokens):
        return "admin"
    if token and _match(token, s.extractor_tokens):
        return "extractor"
    raise HTTPException(401, "Missing or invalid access token.")


def admin_only(role: Role = Depends(auth)) -> Role:
    if role != "admin":
        raise HTTPException(403, "Only authorised (admin) users can open the Client Master.")
    return role


def cipher() -> Cipher:
    try:
        return Cipher()
    except ConfigError as e:
        raise HTTPException(500, str(e)) from e


# ---------------------------------------------------------------- scheduler
_scheduler = None


def start_scheduler() -> None:
    global _scheduler
    s = get_settings()
    if not s.schedule_enabled:
        log.info("Scheduled sync disabled (SYNC_SCHEDULE_ENABLED=false)")
        return
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    _scheduler = BackgroundScheduler(timezone=s.timezone)
    _scheduler.add_job(_scheduled_sync, CronTrigger.from_crontab(s.schedule_cron, timezone=s.timezone),
                       id="finesse_sync", max_instances=1, coalesce=True, misfire_grace_time=3600)
    _scheduler.start()
    log.info("Scheduled Finesse sync: '%s' (%s)", s.schedule_cron, s.timezone)


def _scheduled_sync() -> None:
    try:
        sync.run_sync("schedule")
    except sync.SyncBusy:
        log.info("Scheduled sync skipped — another sync is running")


def next_run() -> str | None:
    if not _scheduler:
        return None
    job = _scheduler.get_job("finesse_sync")
    return job.next_run_time.isoformat() if job and job.next_run_time else None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    with db.connect() as c:
        db.mark_stale_runs(c)
    start_scheduler()
    yield
    if _scheduler:
        _scheduler.shutdown(wait=False)


app = FastAPI(title="Client Master Sync", lifespan=lifespan, docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=get_settings().cors_origins, allow_credentials=False,
                   allow_methods=["GET", "POST", "PUT", "DELETE"], allow_headers=["Authorization", "Content-Type"])


@app.middleware("http")
async def no_cache(request, call_next):
    resp = await call_next(request)
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------- routes
LOOPBACK = {"127.0.0.1", "::1", "localhost"}


@app.get("/")
def index(request: Request):
    """Serve the extractor. Opened on this PC itself, it is pre-connected with the
    (lookup-only) extractor token, so users never have to paste it. The admin token
    is still required for the Client Master screen."""
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    s = get_settings()
    host = request.client.host if request.client else ""
    if host in LOOPBACK and s.extractor_tokens:
        boot = "<script>window.CNE_SERVICE_AUTO=" + json.dumps({"ext": s.extractor_tokens[0]}) + ";</script>"
        html = html.replace("</head>", boot + "\n</head>", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/api/health")
def health():
    return {"ok": True, "service": "client-master-sync"}


@app.get("/api/whoami")
def whoami(role: Role = Depends(auth)):
    return {"role": role}


class ResolveIn(BaseModel):
    file_name: str = ""
    trading_code: str = ""
    client_name: str = ""


@app.post("/api/passwords/resolve")
def resolve(body: ResolveIn, _role: Role = Depends(auth), c: Cipher = Depends(cipher)):
    return master.resolve_passwords(c, body.file_name, body.trading_code, body.client_name)


@app.get("/api/directory")
def get_directory(_role: Role = Depends(auth)):
    return {"clients": master.directory()}


@app.get("/api/sync/status")
def sync_status(_role: Role = Depends(admin_only)):
    return {**sync.status(), "next_scheduled_run": next_run(), "counts": master.counts()}


@app.post("/api/sync", status_code=202)
def sync_now(_role: Role = Depends(admin_only)):
    if sync.status()["running"]:
        raise HTTPException(409, "A sync is already running.")

    def go():
        try:
            sync.run_sync("manual")
        except sync.SyncBusy:
            pass

    threading.Thread(target=go, name="finesse-sync", daemon=True).start()
    return {"started": True}


@app.get("/api/sync/logs")
def sync_logs(limit: int = 30, _role: Role = Depends(admin_only)):
    with db.connect() as conn:
        return {"logs": db.recent_logs(conn, max(1, min(limit, 500)))}


@app.get("/api/exceptional")
def list_exceptional(reveal: bool = False, _role: Role = Depends(admin_only), c: Cipher = Depends(cipher)):
    return {"items": master.list_exceptional(c, reveal=reveal), "counts": master.counts()}


class ExceptionalIn(BaseModel):
    trading_code: str
    password: str
    note: str = ""
    client_name: str = ""
    user: str = ""


@app.post("/api/exceptional")
def add_exceptional(body: ExceptionalIn, _role: Role = Depends(admin_only), c: Cipher = Depends(cipher)):
    try:
        return master.add_exceptional(c, body.trading_code, body.password, body.note, body.user, body.client_name)
    except master.MasterError as e:
        raise HTTPException(400, str(e)) from e


class ExceptionalPatch(BaseModel):
    password: str | None = None
    note: str | None = None


@app.put("/api/exceptional/{row_id}")
def edit_exceptional(row_id: int, body: ExceptionalPatch, _role: Role = Depends(admin_only), c: Cipher = Depends(cipher)):
    try:
        master.update_exceptional(c, row_id, body.password, body.note)
    except master.MasterError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}


@app.delete("/api/exceptional/{row_id}")
def remove_exceptional(row_id: int, _role: Role = Depends(admin_only)):
    try:
        master.delete_exceptional(row_id)
    except master.MasterError as e:
        raise HTTPException(404, str(e)) from e
    return {"ok": True}


@app.post("/api/exceptional/import")
async def import_exceptional(file: UploadFile = File(...), user: str = "", _role: Role = Depends(admin_only),
                             c: Cipher = Depends(cipher)):
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(413, "File too large (max 10 MB).")
    try:
        rows = _read_rows(data, file.filename or "")
        return master.import_exceptional(c, rows, user)
    except master.MasterError as e:
        raise HTTPException(400, str(e)) from e


def _read_rows(data: bytes, name: str) -> list[list[str]]:
    import pandas as pd

    try:
        if name.lower().endswith((".xlsx", ".xls")):
            df = pd.read_excel(io.BytesIO(data), dtype=str, header=None)
        else:
            return read_delimited(data.decode("utf-8-sig", errors="replace"))
    except Exception as e:  # noqa: BLE001
        raise master.MasterError(f"Could not read the file: {e}") from e
    return df.fillna("").astype(str).values.tolist()
