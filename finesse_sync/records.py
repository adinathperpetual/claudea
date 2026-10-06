"""Turn whatever Finesse returns (CSV/Excel export, JSON, HTML table rows) into
clean ``ClientRecord`` objects, auto-detecting the name / PAN / trading-code columns."""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from typing import Any, Iterable

from .config import get_settings

NAME_HDR = re.compile(r"(client\s*_?name|^name$|full\s*_?name|holder|customer\s*_?name|clientname)", re.I)
PAN_HDR = re.compile(r"(^pan$|pan\s*_?(no|number|card)?$|\bpan\b|panno|pan_number|incometax)", re.I)
CODE_HDR = re.compile(
    r"(trading\s*_?(a/?c|account|acct|code)(\s*_?(no|number|code))?|back\s*_?office\s*_?code|"
    r"client\s*_?(code|id)|ucc|clientcode|acc(oun)?t\s*_?(no|number|code)|^code$)", re.I)


class LayoutChangedError(RuntimeError):
    """Finesse's page/report no longer looks like we expect."""


@dataclass
class ClientRecord:
    trading_code: str
    client_name: str
    pan: str


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
    for h in headers:
        if h not in taken and rx.search(str(h)):
            return h
    return None


def detect_columns(headers: list[str]) -> tuple[str, str, str]:
    s = get_settings()
    taken: set[str] = set()
    pan = _pick(headers, s.field_pan, PAN_HDR, taken)
    if pan:
        taken.add(pan)
    code = _pick(headers, s.field_code, CODE_HDR, taken)
    if code:
        taken.add(code)
    name = _pick(headers, s.field_name, NAME_HDR, taken)
    missing = [n for n, v in (("client name", name), ("PAN", pan), ("trading account", code)) if not v]
    if missing:
        raise LayoutChangedError(
            "Could not find the " + ", ".join(missing) + " column(s) in the Finesse data. "
            f"Columns seen: {headers[:30]}. Set FINESSE_FIELD_NAME / FINESSE_FIELD_PAN / "
            "FINESSE_FIELD_TRADING_CODE in .env to the exact column names.")
    return name, pan, code  # type: ignore[return-value]


def from_dicts(rows: Iterable[dict]) -> list[ClientRecord]:
    rows = [r for r in rows if isinstance(r, dict)]
    if not rows:
        return []
    headers: list[str] = []
    for r in rows[:50]:
        for k in r.keys():
            if k not in headers:
                headers.append(k)
    name, pan, code = detect_columns(headers)
    out = []
    for r in rows:
        rec = ClientRecord(_clean(r.get(code)).upper(), _clean(r.get(name)), _clean(r.get(pan)))
        if rec.trading_code or rec.client_name or rec.pan:
            out.append(rec)
    return out


def from_table(headers: list[str], rows: list[list[str]]) -> list[ClientRecord]:
    headers = [_clean(h) or f"col{i}" for i, h in enumerate(headers)]
    return from_dicts([dict(zip(headers, r)) for r in rows])


def from_file_bytes(data: bytes, filename: str = "", content_type: str = "") -> list[ClientRecord]:
    """Parse a CSV / Excel export."""
    import pandas as pd

    name = filename.lower()
    is_excel = name.endswith((".xlsx", ".xls")) or "spreadsheet" in content_type or "excel" in content_type \
        or data[:4] == b"PK\x03\x04" or data[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    try:
        if is_excel:
            raw = pd.read_excel(io.BytesIO(data), dtype=str, header=None)
        else:
            text = data.decode("utf-8-sig", errors="replace")
            if text.lstrip().startswith("<"):
                raise LayoutChangedError("Finesse returned an HTML page instead of a report file (session expired or URL changed).")
            grid = read_delimited(text)
        if is_excel:
            grid = [[_clean(c) for c in row] for row in raw.fillna("").values.tolist()]
    except LayoutChangedError:
        raise
    except Exception as e:  # noqa: BLE001
        raise LayoutChangedError(f"Could not read the Finesse export file: {e}") from e
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
