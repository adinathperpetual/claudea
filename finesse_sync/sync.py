"""Sync service: Finesse -> local Client Master (upsert by trading code)."""
from __future__ import annotations

import logging
import threading
from typing import Callable

from . import db
from .config import ConfigError, get_settings, redact
from .finesse import FinesseClient, FinesseError
from .records import ClientRecord, LayoutChangedError
from .security import Cipher, is_valid_pan, mask_pan, normalize_pan

log = logging.getLogger("finesse_sync.sync")

_run_lock = threading.Lock()
_state = {"running": False, "trigger": None, "started_at": None}


class SyncBusy(RuntimeError):
    pass


def status() -> dict:
    with db.connect() as c:
        last = db.recent_logs(c, 1)
        ok = db.last_successful_sync(c)
    return {"running": _state["running"], "trigger": _state["trigger"], "started_at": _state["started_at"],
            "last_run": last[0] if last else None,
            "last_success_at": ok["ended_at"] if ok else None}


def run_sync(trigger: str = "manual", fetcher: Callable[[], tuple[list[ClientRecord], str]] | None = None) -> dict:
    """Fetch from Finesse and upsert. Never raises for Finesse/data problems — the
    outcome (incl. errors) is written to sync_log and returned. Existing data is
    left untouched when the fetch fails, so the extractor keeps using the last sync."""
    if not _run_lock.acquire(blocking=False):
        raise SyncBusy("A sync is already running.")
    _state.update(running=True, trigger=trigger, started_at=db.now_iso())
    errors: list[str] = []
    stats = dict(fetched=0, added=0, updated=0, unchanged=0, flagged_missing=0, reactivated=0, invalid_pan=0)
    method = None
    status_ = "failed"
    message = ""
    with db.connect() as c:
        log_id = db.start_log(c, trigger)
    try:
        cipher = Cipher()
        if fetcher is None:
            with FinesseClient(cipher=cipher) as fc:
                records, method = fc.fetch_clients()
        else:
            records, method = fetcher()
        stats["fetched"] = len(records)
        log.info("Fetched %d client rows from Finesse via %s", len(records), method)
        status_, message = _apply(records, cipher, stats, errors)
    except (FinesseError, LayoutChangedError, ConfigError) as e:
        for k in ("added", "updated", "unchanged", "flagged_missing", "reactivated"):
            stats[k] = 0                      # the transaction was rolled back
        message = redact(str(e))
        errors.append(message)
        log.error("Sync failed: %s", message)
    except Exception as e:  # noqa: BLE001 — never let a sync crash the service
        message = "Unexpected error: " + redact(f"{type(e).__name__}: {e}")
        errors.append(message)
        log.exception("Sync failed unexpectedly")
    finally:
        with db.connect() as c:
            db.finish_log(c, log_id, status=status_, method=method, stats=stats, errors=errors, message=message)
        _state.update(running=False, trigger=None, started_at=None)
        _run_lock.release()
    with db.connect() as c:
        return db.recent_logs(c, 1)[0]


def _apply(records: list[ClientRecord], cipher: Cipher, stats: dict, errors: list[str]) -> tuple[str, str]:
    s = get_settings()
    # 1) validate / de-duplicate
    clean: dict[str, ClientRecord] = {}
    for i, r in enumerate(records, 1):
        code = r.trading_code.strip().upper()
        if not code:
            errors.append(f"Row {i}: missing trading account ({r.client_name or 'no name'}) — skipped")
            continue
        if not r.client_name:
            errors.append(f"{code}: missing client name — skipped")
            continue
        pan = normalize_pan(r.pan)
        if not is_valid_pan(pan):
            stats["invalid_pan"] += 1
            errors.append(f"{code} ({r.client_name}): invalid PAN {mask_pan(pan) or '(blank)'} — stored but flagged")
        if code in clean:
            errors.append(f"{code}: appears more than once in Finesse — last row kept")
        clean[code] = ClientRecord(code, r.client_name.strip(), pan, (r.trading_account or "").strip().upper())

    if len(clean) < max(1, s.min_records):
        raise FinesseError(f"Finesse returned only {len(clean)} usable clients (minimum {s.min_records}); "
                           "nothing was changed. The page layout or report may have changed.")

    ts = db.now_iso()
    with db.connect() as c:
        existing = db.all_clients(c)
        active_codes = {k for k, v in existing.items() if v["active"]}
        missing = sorted(active_codes - clean.keys())
        if active_codes and len(missing) * 100 > s.max_missing_pct * len(active_codes):
            raise FinesseError(
                f"Finesse returned {len(clean)} clients but {len(missing)} of {len(active_codes)} active clients "
                f"would be flagged missing (> {s.max_missing_pct}%). Sync aborted to protect the master; "
                "check Finesse or raise SYNC_MAX_MISSING_PERCENT.")
        for code, r in clean.items():
            valid = is_valid_pan(r.pan)
            old = existing.get(code)
            if old is None:
                db.insert_client(c, code, r.client_name, cipher.encrypt(r.pan), valid, ts, r.trading_account)
                stats["added"] += 1
                continue
            old_pan = cipher.decrypt(old["pan_enc"]) if old["pan_enc"] else ""
            if old["client_name"] != r.client_name or old_pan != r.pan or (old["trading_account"] or "") != r.trading_account:
                db.update_client(c, code, r.client_name, cipher.encrypt(r.pan), valid, ts, r.trading_account)
                stats["updated"] += 1
                if not old["active"]:
                    stats["reactivated"] += 1
            else:
                db.touch_client(c, code, ts, reactivate=not old["active"])
                if not old["active"]:
                    stats["reactivated"] += 1
                else:
                    stats["unchanged"] += 1
        db.flag_missing(c, missing, ts)
        stats["flagged_missing"] = len(missing)
    status_ = "partial" if errors else "success"
    msg = (f"{stats['fetched']} fetched · {stats['added']} added · {stats['updated']} updated · "
           f"{stats['flagged_missing']} no longer in Finesse · {len(errors)} issue(s)")
    return status_, msg
