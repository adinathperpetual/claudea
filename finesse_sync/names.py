"""Client-name matching — a straight port of the extractor's JS matcher
(normTokens / scoreNames / passwordsForFile in index.html), so the service and
the browser agree on which client a contract-note file belongs to. Tolerates
missing middle names, reversed name order, initials and small typos; a HUF /
company / trust never matches an individual."""
from __future__ import annotations

import re

HONORIFIC = re.compile(r"^(MR|MRS|MS|MISS|SHRI|SHREE|SRI|SMT|DR|PROF|MESSRS|MASTER|KUM)$")
ENTITY_TOKENS = {
    "HUF": re.compile(r"^(HUF)$"),
    "CORP": re.compile(r"^(PVT|PRIVATE|LTD|LIMITED|LLP|INC|CORP|CORPORATION|COMPANY)$"),
    "TRUST": re.compile(r"^(TRUST|TRUSTEE|TRUSTEES)$"),
    "FIRM": re.compile(r"^(AOP|BOI|PARTNERSHIP)$"),
}
AUTO_MATCH, SUGGEST_MATCH = 93, 75


def norm_tokens(raw: str) -> tuple[list[str], str]:
    up = re.sub(r"\s+", " ", re.sub(r"[.,&\-/()_'\"]+", " ", str(raw or "").upper())).strip()
    toks = [t for t in (up.split(" ") if up else []) if not HONORIFIC.match(t)]
    entity, kept = "IND", []
    for t in toks:
        cls = next((c for c, rx in ENTITY_TOKENS.items() if rx.match(t)), None)
        if cls:
            if entity == "IND":
                entity = cls
        else:
            kept.append(t)
    return kept, entity


def _lev(a: str, b: str) -> int:
    if a == b:
        return 0
    m, n = len(a), len(b)
    if not m or not n:
        return max(m, n)
    prev = list(range(n + 1))
    for i in range(1, m + 1):
        cur = [i]
        for j in range(1, n + 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] != b[j - 1])))
        prev = cur
    return prev[n]


def _ratio(a: str, b: str) -> float:
    L = max(len(a), len(b))
    return 1 - _lev(a, b) / L if L else 1


def _token_sim(a: str, b: str) -> float:
    if a == b:
        return 1
    if len(a) == 1 or len(b) == 1:
        ini, full = (a, b) if len(a) == 1 else (b, a)
        return 0.9 if full[0] == ini else 0
    r = _ratio(a, b)
    return r * 0.95 if r >= 0.82 else 0


def _coverage(A: list[str], B: list[str]) -> float:
    short, long = (A, B) if len(A) <= len(B) else (B, A)
    used: set[int] = set()
    total = 0.0
    for t in short:
        best, bi = 0.0, -1
        for i, u in enumerate(long):
            if i in used:
                continue
            s = _token_sim(t, u)
            if s > best:
                best, bi = s, i
        if bi >= 0:
            used.add(bi)
        total += best
    return total / len(short) if short else 0


def score_names(a: str, b: str) -> int:
    A, ea = norm_tokens(a)
    B, eb = norm_tokens(b)
    if not A or not B or ea != eb:
        return 0
    cov = _coverage(A, B)
    score = cov * 100
    if min(len(A), len(B)) == 1:
        score = min(score, 70)
    if len(A) == len(B) and cov > 0.999:
        score = 100
    return int(score + 0.5)   # JS Math.round, not banker's rounding


def simple_norm(s: str) -> str:
    s = re.sub(r"\.(pdf|xlsx?|csv|txt)$", "", str(s or "").lower())
    s = re.sub(r"[._\-]+", " ", s)
    s = re.sub(r"[^a-z0-9 ]+", "", s)
    return re.sub(r"\s+", " ", s).strip()


def name_from_file(file_name: str) -> str:
    """Client name = text after the LAST underscore of the file name (extension stripped)."""
    base = re.sub(r"\.[A-Za-z0-9]+$", "", str(file_name or ""))
    cut = base.rfind("_")
    return (base[cut + 1:] if cut >= 0 else base).strip()


def best_matches(target: str, names: list[str]) -> list[str]:
    """Names in ``names`` that confidently match ``target`` (same rules as the JS tool)."""
    t = simple_norm(target)
    if not t:
        return []
    exact = [n for n in names if simple_norm(n) == t]
    if exact:
        return exact
    scored = sorted(((n, score_names(target, n)) for n in names), key=lambda x: -x[1])
    if not scored:
        return []
    top = scored[0][1]
    if top >= AUTO_MATCH:
        return [n for n, s in scored if s >= AUTO_MATCH and top - s <= 3]
    if top >= SUGGEST_MATCH and (len(scored) == 1 or top - scored[1][1] >= 8):
        return [scored[0][0]]
    return []
