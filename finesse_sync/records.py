"""Turn whatever Finesse returns (CSV/Excel export, JSON, HTML table rows) into
clean ``ClientRecord`` objects, auto-detecting the name / PAN / trading-code columns."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any, Iterable

from .config import get_settings

NAME_HDR = re.compile(r"(client\s*_?name|^name$|full\s*_?name|holder|customer\s*_?name|clientname)", re.I)
PAN_HDR = re.compile(r"(^pan$|pan\s*_?(no|number|card)?$|\bpan\b|panno|pan_number|incometax)", re.I)
# Finesse's own client code (e.g. PCA00141) — the stable key
CODE_HDR = re.compile(r"(client\s*_?(code|id)|clientcode|^code$)", re.I)
# Broker trading account (e.g. D062580) — what contract notes / the template use
ACCT_HDR = re.compile(
    r"(trading\s*_?(a/?c|account|acct|code)(\s*_?(no|number|code))?|back\s*_?office\s*_?code|"
    r"\bucc\b|acc(oun)?t\s*_?(no|number|code)|broker\s*_?code)", re.I)
NOT_CLIENT_HDR = re.compile(
    r"(family|manager|\brm\b|joint|second|third|nominee|guardian|father|mother|spouse|bank|branch|"
    r"\bdp\b|depository|group|introducer|referr|partner|sub\s*broker|ifsc|micr)", re.I)
# "ABK Imports Pvt Ltd (D062580)" -> name + trading account (only when the bracket holds a digit,
# so "(HUF)" stays part of the name)
NAME_ACCT_RE = re.compile(r"^(.*?)\s*\(\s*((?=[^)]*\d)[A-Za-z0-9][A-Za-z0-9\-/]{1,24})\s*\)\s*$")


class LayoutChangedError(RuntimeError):
    """Finesse's page/report no longer looks like we expect."""


@dataclass
class ClientRecord:
    trading_code: str          # Finesse client code (primary key)
    client_name: str
    pan: str
    trading_account: str = ""  # broker trading account, if Finesse shows one


def split_name_account(name: str) -> tuple[str, str]:
    m = NAME_ACCT_RE.match(name or "")
    return (m.group(1).strip(), m.group(2).upper()) if m else ((name or "").strip(), "")


def _clean(v: Any) -> str:
    if v is None:
        return ""
    s = str(v).strip()
    return "" if s.lower() in ("nan", "none", "null") else re.sub(r"\s+", " ", s)


def _pick(headers: list[str], configured: str, rx: re.Pattern, taken: set[str]) -> str | None:
    if configured:
        for h in headers:
            if h.strip().lower() == configured.strip().lower():
                return h
        raise LayoutChangedError(f"Configured column {configured!r} not found in Finesse data. Columns seen: {headers[:30]}")
    # First pass skips other people's / other things' columns ("Joint Holder PAN",
    # "Family Name", "Bank Account No", "RM Name" …); second pass takes any match.
    for strict in (True, False):
        for h in headers:
            if h in taken or not rx.search(str(h)):
                continue
            if strict and NOT_CLIENT_HDR.search(str(h)):
                continue
            return h
    return None


def detect_columns(headers: list[str]) -> tuple[str, str, str, str | None]:
    """Return (name, pan, key, account) column names. ``key`` is the client code column
    (or the trading account column when Finesse has no client code); ``account`` is the
    trading account column, or None (then it is read from "Name (ACCOUNT)")."""
    s = get_settings()
    taken: set[str] = set()
    pan = _pick(headers, s.field_pan, PAN_HDR, taken)
    if pan:
        taken.add(pan)
    acct = _pick(headers, s.field_account, ACCT_HDR, taken)
    if acct:
        taken.add(acct)
    code = _pick(headers, s.field_code, CODE_HDR, taken)
    if code:
        taken.add(code)
    name = _pick(headers, s.field_name, NAME_HDR, taken)
    key = code or acct
    missing = [n for n, v in (("client name", name), ("PAN", pan), ("client code / trading account", key)) if not v]
    if missing:
        raise LayoutChangedError(
            "Could not find the " + ", ".join(missing) + " column(s) in the Finesse data. "
            f"Columns seen: {headers[:30]}. Set FINESSE_FIELD_NAME / FINESSE_FIELD_PAN / "
            "FINESSE_FIELD_TRADING_CODE (client code) / FINESSE_FIELD_TRADING_ACCOUNT in .env to the exact column names.")
    return name, pan, key, (acct if acct != key else None)  # type: ignore[return-value]


def from_dicts(rows: Iterable[dict]) -> list[ClientRecord]:
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return []
    headers: list[str] = []
    for r in rows[:50]:
        for k in r.keys():
            if k not in headers:
                headers.append(k)
    name_col, pan_col, key_col, acct_col = detect_columns(headers)
    out = []
    for r in rows:
        name, in_brackets = split_name_account(_clean(r.get(name_col)))
        acct = _clean(r.get(acct_col)).upper() if acct_col else ""
        acct = acct or in_brackets
        pan = _clean(r.get(pan_col))
        # key: client code; else trading account; else PAN (stable, always present)
        key = _clean(r.get(key_col)).upper() or acct or re.sub(r"\s+", "", pan).upper()
        rec = ClientRecord(key, name, pan, acct)
        if rec.trading_code or rec.client_name or rec.pan:
            out.append(rec)
    return out


def from_table(headers: list[str], rows: list[list[str]]) -> list[ClientRecord]:
    headers = [_clean(h) or f"col{i}" for i, h in enumerate(headers)]
    return from_dicts([dict(zip(headers, r)) for r in rows])


class _TableGrid(HTMLParser):
    """Rows of every HTML table — also Excel 2003 XML (<Row><Cell><Data>), which many
    back-office systems save with an .xls name."""

    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, attrs):
        if tag in ("tr", "row"):
            self._row = []
        elif tag in ("td", "th", "cell") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th", "cell") and self._row is not None and self._cell is not None:
            self._row.append(_clean("".join(self._cell)))
            self._cell = None
        elif tag in ("tr", "row") and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def _markup_grid(text: str) -> list[list[str]]:
    p = _TableGrid()
    p.feed(text)
    return p.rows


def from_file_bytes(data: bytes, filename: str = "", content_type: str = "") -> list[ClientRecord]:
    """Parse a report file: .xlsx / .xls (real or HTML/XML in disguise) / .csv / .txt."""
    import pandas as pd

    name = filename.lower()
    head = data[:2048].lstrip(b"\xef\xbb\xbf \r\n\t").lower()
    is_markup = head.startswith(b"<")
    is_excel = not is_markup and (name.endswith((".xlsx", ".xls", ".xlsm")) or "spreadsheet" in content_type
                                  or "excel" in content_type or data[:4] == b"PK\x03\x04"
                                  or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1")
    try:
        if is_markup:
            grid = _markup_grid(data.decode("utf-8-sig", errors="replace"))
            if not grid or (re.search(rb"type=[\"']?password", data[:200000], re.I) and not any(
                    PAN_HDR.search(" | ".join(r)) for r in grid[:25])):
                raise LayoutChangedError("Finesse returned a web page instead of the report file "
                                         "(session expired or the page changed).")
        elif is_excel:
            raw = pd.read_excel(io.BytesIO(data), dtype=str, header=None)
            grid = [[_clean(c) for c in row] for row in raw.fillna("").values.tolist()]
        else:
            grid = read_delimited(data.decode("utf-8-sig", errors="replace"))
    except LayoutChangedError:
        raise
    except Exception as e:  # noqa: BLE001
        raise LayoutChangedError(f"Could not read the Finesse report file: {e}") from e
    # Reports often have title rows above the header: use the first row that looks like the header.
    for i, row in enumerate(grid[:25]):
        joined = " | ".join(row)
        if PAN_HDR.search(joined) and NAME_HDR.search(joined):
            return from_table(row, grid[i + 1:])
    if not grid:
        return []
    return from_table(grid[0], grid[1:])


def read_delimited(text: str) -> list[list[str]]:
    """CSV/TSV with possible title lines above the header (ragged rows are fine)."""
    lines = [l for l in text.splitlines() if l.strip()]
    best = max((",", "\t", ";", "|"), key=lambda d: max((l.count(d) for l in lines[:30]), default=0))
    return [[_clean(c) for c in row] for row in csv.reader(lines, delimiter=best)]


def find_records_in_json(payload: Any, path: str = "") -> list[dict]:
    """Locate the list of client objects in a JSON response.
    ``path`` (e.g. ``data.items``) wins; otherwise the largest list of dicts is used."""
    if path:
        cur = payload
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            elif isinstance(cur, list) and part.isdigit():
                cur = cur[int(part)]
            else:
                raise LayoutChangedError(f"FINESSE_API_RECORDS_PATH {path!r} not found in the Finesse response.")
        if not isinstance(cur, list):
            raise LayoutChangedError(f"FINESSE_API_RECORDS_PATH {path!r} does not point to a list.")
        return cur

    best: list[dict] = []

    def walk(node: Any, depth: int = 0) -> None:
        nonlocal best
        if depth > 6:
            return
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node[:20]) and len(node) > len(best):
                best = node
            for x in node[:5]:
                walk(x, depth + 1)
        elif isinstance(node, dict):
            for v in node.values():
                walk(v, depth + 1)

    walk(payload)
    return best
