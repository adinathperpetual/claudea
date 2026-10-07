"""Exceptional-password list + password resolution for contract-note PDFs.

Default rule: the PDF password is the client's PAN in uppercase.
Only clients whose password does NOT follow that rule are stored here
("exceptional"), keyed by trading code.

Resolution for a PDF: identify the client (trading code in the request, a
trading code inside the file name, or the client name at the end of the file
name) -> exceptional password(s) for that trading code if any, otherwise the
default rule (PAN uppercase).
"""
from __future__ import annotations

import re

from . import db
from .names import best_matches, name_from_file
from .records import ACCT_HDR, CODE_HDR, NAME_HDR, _clean
from .security import Cipher, default_password, is_valid_pan, mask_pan, mask_secret

PW_HDR = re.compile(r"(password|pass\s*word|\bpwd\b|\bpass\b|passcode|\bpin\b)", re.I)


class MasterError(ValueError):
    pass


# ---------------------------------------------------------------- listing
def list_exceptional(cipher: Cipher, reveal: bool = False) -> list[dict]:
    """One entry per exceptional password, joined with the Finesse client row."""
    with db.connect() as c:
        clients = db.all_clients(c)
        rows = db.exceptional_rows(c)
    out = []
    for r in rows:
        cl = clients.get(r["trading_code"])
        pan = cipher.decrypt(cl["pan_enc"]) if cl and cl["pan_enc"] else ""
        pw = cipher.decrypt(r["password_enc"])
        if not cl:
            status = "not_in_finesse"
        elif not cl["active"]:
            status = "missing_from_finesse"
        else:
            status = "active"
        out.append({
            "id": r["id"],
            "trading_code": r["trading_code"],
            "trading_account": (cl["trading_account"] if cl else "") or "",
            "client_name": (cl["client_name"] if cl else None) or r["client_name"] or "",
            "pan_masked": mask_pan(pan),
            "pan_valid": bool(cl["pan_valid"]) if cl else False,
            "password": pw if reveal else mask_secret(pw),
            "note": r["note"] or "",
            "status": status,
            "missing_since": cl["missing_since"] if cl else None,
            "updated_at": r["updated_at"],
            "created_by": r["created_by"] or "",
        })
    return out


def directory(include_inactive: bool = False) -> list[dict]:
    """Name + broker trading account of every client that has one (no PAN) — used by the
    extractor to fill the Trading Account column."""
    with db.connect() as c:
        q = ("SELECT trading_code, client_name, trading_account, active FROM clients WHERE trading_account != ''"
             + ("" if include_inactive else " AND active=1"))
        return [{"trading_code": r["trading_code"], "trading_account": r["trading_account"], "name": r["client_name"],
                 "active": bool(r["active"])} for r in c.execute(q + " ORDER BY client_name")]


def counts() -> dict:
    with db.connect() as c:
        one = lambda q: c.execute(q).fetchone()[0]  # noqa: E731
        return {"clients_total": one("SELECT COUNT(*) FROM clients"),
                "clients_active": one("SELECT COUNT(*) FROM clients WHERE active=1"),
                "clients_missing": one("SELECT COUNT(*) FROM clients WHERE active=0"),
                "invalid_pan": one("SELECT COUNT(*) FROM clients WHERE pan_valid=0"),
                "exceptional": one("SELECT COUNT(DISTINCT trading_code) FROM exceptional_passwords")}


# ---------------------------------------------------------------- edits
def add_exceptional(cipher: Cipher, trading_code: str, password: str, note: str = "", user: str = "",
                    client_name: str = "") -> dict:
    code = (trading_code or "").strip().upper()
    pw = (password or "").strip()
    if not code or not pw:
        raise MasterError("Client code / trading account and password are both required.")
    with db.connect() as c:
        code = db.code_for(c, code) or code      # a trading account is stored under its client code
        cl = db.get_client(c, code)
        if cl and cl["pan_enc"] and pw == default_password(cipher.decrypt(cl["pan_enc"])):
            raise MasterError(f"{code}: that password is the client's PAN — the default rule already covers it, "
                              "so it is not exceptional.")
        for r in db.exceptional_rows(c, code):
            if cipher.decrypt(r["password_enc"]) == pw:
                raise MasterError(f"{code}: that password is already on the list.")
        row_id = db.add_exceptional(c, code, client_name or (cl["client_name"] if cl else ""), cipher.encrypt(pw), note, user)
    return {"id": row_id, "trading_code": code, "known_client": bool(cl)}


def update_exceptional(cipher: Cipher, row_id: int, password: str | None, note: str | None) -> None:
    enc = cipher.encrypt(password.strip()) if password else None
    with db.connect() as c:
        if not db.update_exceptional(c, row_id, enc, note):
            raise MasterError("Entry not found.")


def delete_exceptional(row_id: int) -> None:
    with db.connect() as c:
        if not db.delete_exceptional(c, row_id):
            raise MasterError("Entry not found.")


def import_exceptional(cipher: Cipher, rows: list[list[str]], user: str = "") -> dict:
    """Import the team's existing master (Client Name | Trading Account | Password [...]).
    Rows whose password equals the client's PAN are skipped (default rule covers them);
    every other password is stored as exceptional."""
    rows = [[_clean(x) for x in r] for r in rows if any(_clean(x) for x in r)]
    if not rows:
        raise MasterError("The file is empty.")
    head = rows[0]
    pw_cols = [i for i, h in enumerate(head) if PW_HDR.search(h)]
    code_col = next((i for i, h in enumerate(head) if i not in pw_cols and (ACCT_HDR.search(h) or CODE_HDR.search(h))), -1)
    name_col = next((i for i, h in enumerate(head) if i not in pw_cols and i != code_col and NAME_HDR.search(h)), -1)
    if code_col < 0 or not pw_cols:
        raise MasterError("Need a header row with a Trading Account column and at least one Password column.")
    res = {"added": 0, "skipped_default": 0, "skipped_duplicate": 0, "no_code": 0, "unknown_code": 0, "errors": []}
    with db.connect() as c:
        clients = db.all_clients(c)
        name_to_code = {cl["client_name"].upper(): code for code, cl in clients.items()}
        for i, r in enumerate(rows[1:], 2):
            get = lambda k: r[k] if 0 <= k < len(r) else ""  # noqa: E731
            name = get(name_col)
            codes = [db.code_for(c, x) or x.strip().upper() for x in re.split(r"[,;]", get(code_col)) if x.strip()]
            if not codes and name.upper() in name_to_code:
                codes = [name_to_code[name.upper()]]
            if not codes:
                res["no_code"] += 1
                res["errors"].append(f"Row {i} ({name or 'no name'}): no trading account — skipped")
                continue
            for code in codes:
                cl = clients.get(code)
                if not cl:
                    res["unknown_code"] += 1
                pan = cipher.decrypt(cl["pan_enc"]) if cl and cl["pan_enc"] else ""
                existing = {cipher.decrypt(e["password_enc"]) for e in db.exceptional_rows(c, code)}
                for k in pw_cols:
                    pw = get(k).strip()
                    if not pw:
                        continue
                    if pan and pw == default_password(pan):
                        res["skipped_default"] += 1
                        continue
                    if pw in existing:
                        res["skipped_duplicate"] += 1
                        continue
                    db.add_exceptional(c, code, name or (cl["client_name"] if cl else ""), cipher.encrypt(pw),
                                       "imported", user)
                    existing.add(pw)
                    res["added"] += 1
    return res


# ---------------------------------------------------------------- resolution
def resolve_passwords(cipher: Cipher, file_name: str = "", trading_code: str = "", client_name: str = "") -> dict:
    """Candidate passwords for one contract note, best first."""
    with db.connect() as c:
        clients = db.all_clients(c)
        codes: list[str] = []
        how = ""
        tc = (trading_code or "").strip().upper()
        if tc:
            codes, how = [db.code_for(c, tc) or tc], "trading_code"
        if not codes and file_name:
            tokens = {t.upper() for t in re.split(r"[^A-Za-z0-9]+", re.sub(r"\.[A-Za-z0-9]+$", "", file_name)) if t}
            hit = [code for code, cl in clients.items()
                   if code in tokens or (cl["trading_account"] and cl["trading_account"].upper() in tokens)]
            if len(hit) == 1:
                codes, how = hit, "code_in_file_name"
        if not codes:
            target = client_name or name_from_file(file_name)
            by_name: dict[str, list[str]] = {}
            for code, cl in clients.items():
                by_name.setdefault(cl["client_name"], []).append(code)
            # active clients first: a client who left Finesse rarely sends new notes
            names = best_matches(target, list(by_name))
            codes = [code for n in names for code in by_name[n]]
            codes.sort(key=lambda k: (not clients[k]["active"], k))
            how = "client_name" if codes else ""
            if not codes:   # exceptional entries for codes not (yet) in Finesse, matched by stored name
                exc_names: dict[str, list[str]] = {}
                for r in db.exceptional_rows(c):
                    if r["client_name"]:
                        exc_names.setdefault(r["client_name"], []).append(r["trading_code"])
                for n in best_matches(target, list(exc_names)):
                    codes.extend(x for x in exc_names[n] if x not in codes)
                how = "client_name" if codes else ""
        passwords: list[str] = []
        sources: list[str] = []
        matched = []
        for code in codes[:5]:
            cl = clients.get(code)
            matched.append({"trading_code": code, "client_name": cl["client_name"] if cl else "",
                            "trading_account": (cl["trading_account"] if cl else "") or ""})
            exc = [cipher.decrypt(r["password_enc"]) for r in db.exceptional_rows(c, code)]
            for pw in exc:
                if pw and pw not in passwords:
                    passwords.append(pw)
                    sources.append("exceptional")
            if not exc and cl and cl["pan_enc"]:
                pan = cipher.decrypt(cl["pan_enc"])
                if pan and is_valid_pan(pan) and default_password(pan) not in passwords:
                    passwords.append(default_password(pan))
                    sources.append("default")
    return {"matched_by": how, "clients": matched, "passwords": passwords, "sources": sources}

