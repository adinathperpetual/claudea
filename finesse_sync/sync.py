"""Sync service: Finesse -> local Client Master (upsert by trading code)."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
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


def run_sync(trigger: str = "manual", fetcher: Callable[[], tuple[list[ClientRecord], str]] | None = None,
             account_lookup: Callable[[list[str]], dict[str, list[str]]] | None = None) -> dict:
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
        checked: set[str] = set()
        if fetcher is None:
            with FinesseClient(cipher=cipher) as fc:
                records, method = fc.fetch_clients()
                log.info("Fetched %d client rows from Finesse via %s", len(records), method)
                if get_settings().profile_lookup:
                    checked = _fill_missing_accounts(records, account_lookup or fc.lookup_trading_accounts, errors)
        else:
            records, method = fetcher()
            if account_lookup:
                checked = _fill_missing_accounts(records, account_lookup, errors)
        stats["fetched"] = len(records)
        status_, message = _apply(records, cipher, stats, errors)
        if checked:
            with db.connect() as c:
                db.mark_account_checked(c, sorted(checked), db.now_iso())
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


def _fill_missing_accounts(records: list[ClientRecord], lookup: Callable[[list[str]], dict[str, list[str]]],
                           errors: list[str]) -> set[str]:
    """For clients without a trading account in the list/report, read it from their
    Finesse profile. Accounts found earlier are reused; profiles without one are
    re-checked only every FINESSE_PROFILE_RECHECK_DAYS days. Never fails the sync."""
    s = get_settings()
    with db.connect() as c:
        known = {r["trading_code"]: (r["trading_account"] or "", r["account_checked_at"])
                 for r in c.execute("SELECT trading_code, trading_account, account_checked_at FROM clients")}
    cutoff = (datetime.now(timezone.utc) - timedelta(days=s.profile_recheck_days)).isoformat()
    todo = []
    for r in records:
        code = r.trading_code.strip().upper()
        if r.trading_account or not code:
            continue
        acct, checked_at = known.get(code, ("", None))
        if acct:
            r.trading_account = acct                 # found on an earlier sync
        elif not checked_at or checked_at < cutoff:
            todo.append(code)
    if not todo:
        return set()
    if len(todo) > s.profile_lookup_limit:
        errors.append(f"{len(todo)} clients need a profile lookup for their trading account; "
                      f"{s.profile_lookup_limit} done now, the rest on the next syncs")
        todo = todo[:s.profile_lookup_limit]
    log.info("Reading the trading account of %d client(s) from their Finesse profile…", len(todo))
    try:
        found = lookup(todo)
    except Exception as e:  # noqa: BLE001
        errors.append("Trading account lookup on client profiles failed: " + redact(f"{e}")[:300])
        return set()
    by_code = {r.trading_code.strip().upper(): r for r in records}
    for code, accts in found.items():
        if accts and code in by_code:
            by_code[code].trading_account = ", ".join(accts)
    none = [c for c in todo if c in found and not found[c]]
    unread = [c for c in todo if c not in found]
    log.info("Profiles read: %d, accounts found: %d, no account shown: %d, not reachable: %d",
             len(found), sum(1 for a in found.values() if a), len(none), len(unread))
    if unread:
        errors.append(f"Could not open the profile of {len(unread)} client(s): {', '.join(unread[:20])}")
    return set(found)


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
            if not r.trading_account and old["trading_account"]:
                r = ClientRecord(r.trading_code, r.client_name, r.pan, old["trading_account"])
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
