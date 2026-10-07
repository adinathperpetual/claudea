"""Finesse login + client-list fetch.

Fetch strategies, in order of preference (``FINESSE_FETCH_METHOD=auto``):
  1. export      — download the client-list report (CSV/Excel) from FINESSE_EXPORT_URL
  2. api         — call the internal JSON endpoint the web page uses (FINESSE_API_URL)
  3. playwright  — drive a real browser: log in, open the client list, page through the table

The login session (cookies + any bearer token) is stored encrypted on disk and reused
until it expires; an expired session triggers one automatic re-login. Network
failures are retried with exponential backoff. Credentials are never logged.
"""
from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, TypeVar
from urllib.parse import urljoin, urlparse

import httpx

from .config import Settings, get_settings, redact
from .records import ClientRecord, LayoutChangedError, detect_columns, find_records_in_json, from_dicts, from_file_bytes
from .security import Cipher

log = logging.getLogger("finesse_sync.finesse")
T = TypeVar("T")

USER_HINT = re.compile(r"(user|login|client.?id|email|uid|username|userid|mobile|employee|emp.?code|\bid\b)", re.I)
PAN_HINT = re.compile(r"\bpan\b|pan.?(no|number|card)?$|^pan", re.I)
SUBMIT_TEXT = re.compile(r"^\s*(log\s*-?\s*in|sign\s*-?\s*in|submit|continue|proceed|next|verify)\s*$", re.I)
ERROR_TEXT = re.compile(r"(invalid|incorrect|wrong|failed|locked|not\s+match|unauthori[sz]ed|blocked|expired)", re.I)
NEXT_TEXT = re.compile(r"^\s*(next|›|»|>|>>|next\s*page)\s*$", re.I)


class FinesseError(RuntimeError):
    """Any failure talking to Finesse. Message is safe to show to users."""


class LoginError(FinesseError):
    pass


class SessionExpired(FinesseError):
    pass


class NetworkError(FinesseError):
    pass


# ------------------------------------------------------------------ retry
def with_retry(fn: Callable[[], T], *, label: str, attempts: int | None = None, base_delay: float = 2.0,
               sleep: Callable[[float], None] = time.sleep) -> T:
    """Retry ``fn`` on network failures with exponential backoff (2s, 4s, 8s, 16s…)."""
    attempts = attempts or max(1, get_settings().retries)
    last: Exception | None = None
    for i in range(attempts):
        try:
            return fn()
        except (httpx.TransportError, NetworkError) as e:
            last = e
        except httpx.HTTPStatusError as e:
            if e.response.status_code < 500 and e.response.status_code != 429:
                raise
            last = e
        except Exception as e:  # playwright network/timeouts
            if type(e).__name__ not in ("TimeoutError", "Error") or not _is_network_msg(str(e)):
                raise
            last = e
        if i < attempts - 1:
            delay = base_delay * (2 ** i)
            log.warning("%s failed (%s) — retrying in %.0fs (%d/%d)", label, type(last).__name__, delay, i + 1, attempts - 1)
            sleep(delay)
    raise NetworkError(f"{label}: could not reach Finesse after {attempts} attempts ({redact(str(last))[:200]})")


def _is_network_msg(msg: str) -> bool:
    return bool(re.search(r"net::|ERR_|ECONN|ETIMEDOUT|ENOTFOUND|Timeout|timed out|connection", msg, re.I))


# ------------------------------------------------------------------ session store
@dataclass
class SessionState:
    cookies: list[dict] = field(default_factory=list)
    bearer: str = ""
    created_at: float = 0.0

    def age_minutes(self) -> float:
        return (time.time() - self.created_at) / 60 if self.created_at else 1e9


class SessionStore:
    def __init__(self, settings: Settings, cipher: Cipher):
        self.path = settings.data_dir / "finesse_session.enc"
        self.cipher = cipher

    def load(self) -> SessionState | None:
        try:
            raw = self.path.read_text()
            d = json.loads(self.cipher.decrypt(raw))
            return SessionState(d.get("cookies", []), d.get("bearer", ""), d.get("created_at", 0.0))
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001 — corrupt / old key: just log in again
            log.info("Stored Finesse session unreadable — will log in again")
            return None

    def save(self, st: SessionState) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"cookies": st.cookies, "bearer": st.bearer, "created_at": st.created_at})
        self.path.write_text(self.cipher.encrypt(payload) or "")
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


# ------------------------------------------------------------------ client
class FinesseClient:
    def __init__(self, settings: Settings | None = None, cipher: Cipher | None = None):
        self.s = settings or get_settings()
        self.cipher = cipher or Cipher()
        self.store = SessionStore(self.s, self.cipher)
        self.session: SessionState | None = None
        self._http: httpx.Client | None = None

    # ---------------- lifecycle
    def close(self) -> None:
        if self._http:
            self._http.close()
            self._http = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    @property
    def http(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(follow_redirects=True, timeout=self.s.http_timeout,
                                      headers={"User-Agent": "Mozilla/5.0 (ClientMasterSync)",
                                               "Accept": "application/json, text/plain, */*"})
            self._apply_session()
        return self._http

    def _apply_session(self) -> None:
        if not self._http or not self.session:
            return
        self._http.cookies.clear()
        for c in self.session.cookies:
            self._http.cookies.set(c["name"], c["value"], domain=c.get("domain", "").lstrip(".") or None, path=c.get("path", "/"))
        if self.session.bearer:
            self._http.headers["Authorization"] = self.session.bearer
        else:
            self._http.headers.pop("Authorization", None)

    def ensure_session(self) -> None:
        if self.session is None:
            self.session = self.store.load()
        if self.session is None or self.session.age_minutes() > self.s.session_max_age_minutes:
            self.login()
        else:
            self._apply_session()

    def login(self) -> None:
        self.s.require_finesse_credentials()
        log.info("Logging in to Finesse…")
        st = self._http_login() if self.s.finesse_login_api_url else self._browser_login()
        st.created_at = time.time()
        self.session = st
        self.store.save(st)
        self._apply_session()
        log.info("Finesse login OK")

    # ---------------- login: pure HTTP (optional)
    def _http_login(self) -> SessionState:
        f_user, f_pw, f_pan = (self.s.finesse_login_api_fields + ["userId", "password", "pan"])[:3]
        body = {f_user: self.s.finesse_user_id, f_pw: self.s.finesse_password, f_pan: self.s.finesse_pan}

        def go() -> httpx.Response:
            if self.s.finesse_login_api_format == "form":
                return self.http.post(self.s.finesse_login_api_url, data=body)
            return self.http.post(self.s.finesse_login_api_url, json=body)

        r = with_retry(go, label="Finesse login")
        if r.status_code in (400, 401, 403):
            raise LoginError(f"Finesse login failed (HTTP {r.status_code}). Check FINESSE_USER_ID, FINESSE_PASSWORD and FINESSE_PAN.")
        if r.status_code >= 400:
            raise LoginError(f"Finesse login endpoint returned HTTP {r.status_code}.")
        bearer = ""
        try:
            data = r.json()
            if isinstance(data, dict):
                if data.get("success") is False or str(data.get("status", "")).lower() in ("fail", "failed", "error"):
                    raise LoginError("Finesse rejected the login: " + redact(str(data.get("message") or data.get("error") or "invalid credentials"))[:200])
                tok = _find_token(data)
                if tok:
                    bearer = "Bearer " + tok
        except ValueError:
            pass
        cookies = [{"name": c.name, "value": c.value, "domain": c.domain or "", "path": c.path or "/"}
                   for c in self.http.cookies.jar]
        if not cookies and not bearer:
            raise LoginError("Finesse login returned no session cookie or token — check FINESSE_LOGIN_API_URL / FINESSE_LOGIN_API_FIELDS.")
        return SessionState(cookies, bearer)

    # ---------------- login: real browser
    def _browser_login(self) -> SessionState:
        with BrowserSession(self.s) as b:
            b.login()
            return b.session_state()

    # ---------------- fetching
    def fetch_clients(self) -> tuple[list[ClientRecord], str]:
        """Return (records, method used). Raises FinesseError with a clear message."""
        self.s.require_finesse_credentials()
        method = self.s.finesse_fetch_method
        plan: list[tuple[str, Callable[[], list[ClientRecord]]]] = []
        if method in ("auto", "export") and self.s.finesse_export_url:
            plan.append(("export", self._fetch_export))
        if method in ("auto", "api") and self.s.finesse_api_url:
            plan.append(("api", self._fetch_api))
        if method in ("auto", "report") and self.s.finesse_report_name:
            plan.append(("report", self._fetch_report))
        if method in ("auto", "playwright"):
            plan.append(("playwright", self._fetch_browser))
        if not plan:
            raise FinesseError(f"FINESSE_FETCH_METHOD={method} but its URL is not configured "
                               "(FINESSE_EXPORT_URL / FINESSE_API_URL).")
        errors = []
        for name, fn in plan:
            try:
                recs = self._with_relogin(fn) if name in ("export", "api") else fn()
                if recs:
                    return recs, name
                errors.append(f"{name}: returned no clients")
            except LoginError:
                raise
            except (FinesseError, LayoutChangedError) as e:
                log.warning("Fetch via %s failed: %s", name, e)
                errors.append(f"{name}: {e}")
        raise FinesseError("Could not fetch the client list from Finesse. " + " | ".join(errors))

    def _with_relogin(self, fn: Callable[[], list[ClientRecord]]) -> list[ClientRecord]:
        self.ensure_session()
        try:
            return fn()
        except SessionExpired:
            log.info("Finesse session expired — logging in again")
            self.store.clear()
            self.login()
            return fn()

    def _request(self, method: str, url: str, **kw) -> httpx.Response:
        def go():
            r = self.http.request(method, url, **kw)
            if r.status_code >= 500 or r.status_code == 429:
                r.raise_for_status()
            return r
        r = with_retry(go, label="Finesse request")
        _check_not_logged_out(r)
        return r

    def _fetch_export(self) -> list[ClientRecord]:
        url = urljoin(self.s.base_url, self.s.finesse_export_url)
        r = self._request("GET", url)
        if r.status_code >= 400:
            raise FinesseError(f"Export download returned HTTP {r.status_code}")
        fname = _filename(r) or urlparse(url).path
        return from_file_bytes(r.content, fname, r.headers.get("content-type", ""))

    def _fetch_api(self) -> list[ClientRecord]:
        url = urljoin(self.s.base_url, self.s.finesse_api_url)
        out: list[dict] = []
        page, size = self.s.finesse_api_first_page, self.s.finesse_api_page_size
        seen_first: str | None = None
        for _ in range(10000):
            params = {}
            if self.s.finesse_api_page_param:
                params[self.s.finesse_api_page_param] = page
            if self.s.finesse_api_size_param:
                params[self.s.finesse_api_size_param] = size
            if self.s.finesse_api_method == "POST":
                r = self._request("POST", url, json=params)
            else:
                r = self._request("GET", url, params=params)
            if r.status_code >= 400:
                raise FinesseError(f"Client-list API returned HTTP {r.status_code}")
            try:
                payload = r.json()
            except ValueError as e:
                raise SessionExpired("Client-list API did not return JSON (session expired?)") from e
            rows = find_records_in_json(payload, self.s.finesse_api_records_path)
            if not rows:
                break
            key = json.dumps(rows[0], sort_keys=True, default=str)
            if key == seen_first:          # API ignores the page param → stop
                break
            seen_first = key
            out.extend(rows)
            if not self.s.finesse_api_page_param or len(rows) < size:
                break
            page += 1
        return from_dicts(out)

    def _in_browser(self, work: Callable[["BrowserSession"], list[ClientRecord]]) -> list[ClientRecord]:
        if self.session is None:
            self.session = self.store.load()      # reuse saved cookies if still valid
        with BrowserSession(self.s, self.session) as b:
            if not b.is_logged_in():
                b.login()
            recs = work(b)
            st = b.session_state()
            st.created_at = time.time()
            self.session = st
            self.store.save(st)
            return recs

    def _fetch_browser(self) -> list[ClientRecord]:
        return self._in_browser(lambda b: b.read_client_list())

    def lookup_trading_accounts(self, codes: list[str]) -> dict[str, list[str]]:
        found: dict[str, list[str]] = {}

        def work(b: "BrowserSession"):
            found.update(b.trading_accounts_from_profiles(codes))
            return []
        self._in_browser(work)
        return found

    def _fetch_report(self) -> list[ClientRecord]:
        return self._in_browser(lambda b: b.download_client_master_report())


def _find_token(d: Any, depth: int = 0) -> str:
    if depth > 4:
        return ""
    if isinstance(d, dict):
        for k, v in d.items():
            if isinstance(v, str) and re.fullmatch(r"(access_?token|token|jwt|id_?token|auth_?token)", k, re.I) and len(v) > 15:
                return v
        for v in d.values():
            t = _find_token(v, depth + 1)
            if t:
                return t
    return ""


def _filename(r: httpx.Response) -> str:
    cd = r.headers.get("content-disposition", "")
    m = re.search(r'filename\*?=(?:UTF-8\'\')?"?([^";]+)', cd, re.I)
    return m.group(1) if m else ""


def _check_not_logged_out(r: httpx.Response) -> None:
    if r.status_code in (401, 403, 419, 440):
        raise SessionExpired(f"Finesse returned HTTP {r.status_code}")
    final = str(r.url).lower()
    if re.search(r"/(login|signin|sign-in|logon)\b", final) or "sessionexpired" in final:
        raise SessionExpired("Finesse redirected to the login page")
    ctype = r.headers.get("content-type", "")
    if "text/html" in ctype and re.search(rb'type=["\']?password', r.content[:200000], re.I):
        raise SessionExpired("Finesse returned its login page")


# ------------------------------------------------------------------ browser automation
class BrowserSession:
    """Thin Playwright wrapper used for login, the table-scraping fallback and `discover`."""

    def __init__(self, settings: Settings, session: SessionState | None = None, headless: bool | None = None,
                 capture: bool = False):
        self.s = settings
        self.initial = session
        self.headless = settings.finesse_headless if headless is None else headless
        self.capture = capture
        self.captured: list[dict] = []
        self.bearer = session.bearer if session else ""

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise FinesseError("Playwright is not installed: pip install playwright && playwright install chromium") from e
        self._pw = sync_playwright().start()
        try:
            self.browser = self._pw.chromium.launch(headless=self.headless,
                                                    executable_path=self.s.finesse_browser_path or None)
        except Exception as e:  # noqa: BLE001
            self._pw.stop()
            raise FinesseError(f"Could not start Chromium for Playwright ({e}). Run: playwright install chromium") from e
        self.ctx = self.browser.new_context(accept_downloads=True)
        self.ctx.set_default_timeout(self.s.http_timeout * 1000)
        if self.initial and self.initial.cookies:
            try:
                self.ctx.add_cookies([{k: v for k, v in c.items() if k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite")}
                                      for c in self.initial.cookies if c.get("domain")])
            except Exception:  # noqa: BLE001
                pass
        self.downloads: list = []
        self.ctx.on("page", lambda p: p.on("download", lambda d: self.downloads.append(d)))
        self.page = self.ctx.new_page()
        host = urlparse(self.s.finesse_base_url).hostname or ""
        self.page.on("request", lambda req: self._on_request(req, host))
        if self.capture:
            self.page.on("response", self._on_response)
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
            self.browser.close()
        finally:
            self._pw.stop()

    def _on_request(self, req, host: str) -> None:
        try:
            if urlparse(req.url).hostname == host:
                auth = req.headers.get("authorization", "")
                if auth.lower().startswith("bearer "):
                    self.bearer = auth
        except Exception:  # noqa: BLE001
            pass

    def _on_response(self, resp) -> None:
        try:
            req = resp.request
            if req.resource_type not in ("xhr", "fetch", "document", "other"):
                return
            entry = {"url": resp.url, "method": req.method, "status": resp.status,
                     "content_type": resp.headers.get("content-type", ""), "resource_type": req.resource_type}
            pd = req.post_data or ""
            entry["post_data"] = redact(pd)[:2000] if pd else ""
            if "json" in entry["content_type"]:
                try:
                    body = resp.json()
                    rows = find_records_in_json(body)
                    entry["records_found"] = len(rows)
                    if rows:
                        entry["sample_keys"] = list(rows[0].keys())[:40]
                        try:
                            detect_columns(entry["sample_keys"])
                            entry["looks_like_client_list"] = True
                        except LayoutChangedError:
                            entry["looks_like_client_list"] = False
                    if isinstance(body, dict):
                        entry["top_level_keys"] = list(body.keys())[:40]
                except Exception:  # noqa: BLE001
                    pass
            self.captured.append(entry)
        except Exception:  # noqa: BLE001
            pass

    def session_state(self) -> SessionState:
        cookies = [{k: c[k] for k in ("name", "value", "domain", "path", "expires", "httpOnly", "secure", "sameSite") if k in c}
                   for c in self.ctx.cookies()]
        bearer = self.bearer or self._storage_token()
        return SessionState(cookies, bearer)

    def _storage_token(self) -> str:
        try:
            tok = self.page.evaluate("""() => {
              const pick = s => { for (let i=0;i<s.length;i++){ const k=s.key(i), v=s.getItem(k)||"";
                if (/token|jwt|auth/i.test(k) && v.length>15) return v.replace(/^"|"$/g,""); } return ""; };
              try { return pick(window.localStorage) || pick(window.sessionStorage); } catch(e){ return ""; } }""")
            return ("Bearer " + tok) if tok and not tok.startswith("{") else ""
        except Exception:  # noqa: BLE001
            return ""

    # ---- helpers
    def _first_visible(self, selectors: list[str]):
        for sel in selectors:
            if not sel:
                continue
            try:
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 10)):
                    el = loc.nth(i)
                    if el.is_visible():
                        return el
            except Exception:  # noqa: BLE001
                continue
        return None

    def _input_matching(self, rx: re.Pattern, exclude_password: bool = True):
        inputs = self.page.locator("input")
        for i in range(min(inputs.count(), 40)):
            el = inputs.nth(i)
            try:
                if not el.is_visible():
                    continue
                typ = (el.get_attribute("type") or "text").lower()
                if typ in ("hidden", "checkbox", "radio", "submit", "button") or (exclude_password and typ == "password"):
                    continue
                attrs = " ".join(filter(None, [el.get_attribute(a) for a in ("name", "id", "placeholder", "aria-label", "formcontrolname", "autocomplete")]))
                label = ""
                eid = el.get_attribute("id")
                if eid:
                    lab = self.page.locator(f'label[for="{eid}"]')
                    if lab.count():
                        label = lab.first.inner_text()
                if rx.search(attrs) or rx.search(label):
                    return el
            except Exception:  # noqa: BLE001
                continue
        return None

    def _submit(self) -> None:
        btn = self._first_visible([self.s.sel_submit, "button[type=submit]", "input[type=submit]"])
        if btn is None:
            buttons = self.page.locator("button, a[role=button], input[type=button]")
            for i in range(min(buttons.count(), 30)):
                b = buttons.nth(i)
                try:
                    txt = (b.inner_text() or b.get_attribute("value") or "").strip()
                    if b.is_visible() and SUBMIT_TEXT.match(txt):
                        btn = b
                        break
                except Exception:  # noqa: BLE001
                    continue
        if btn is not None:
            btn.click()
        else:
            self.page.keyboard.press("Enter")
        try:
            self.page.wait_for_load_state("networkidle", timeout=self.s.http_timeout * 1000)
        except Exception:  # noqa: BLE001
            pass

    def _password_visible(self) -> bool:
        return self._first_visible(["input[type=password]"]) is not None

    def _visible_error(self) -> str:
        try:
            for sel in (".error", ".alert", ".alert-danger", ".invalid-feedback", "[role=alert]", ".toast", ".mat-error", ".text-danger"):
                loc = self.page.locator(sel)
                for i in range(min(loc.count(), 5)):
                    el = loc.nth(i)
                    if el.is_visible():
                        t = el.inner_text().strip()
                        if t and ERROR_TEXT.search(t):
                            return redact(t)[:200]
        except Exception:  # noqa: BLE001
            pass
        return ""

    def is_logged_in(self) -> bool:
        if not (self.initial and self.initial.cookies):
            return False
        self._goto(urljoin(self.s.base_url, self.s.finesse_client_list_url or ""), "Open Finesse")
        # a single-page app decides a moment later whether to show the login form
        for _ in range(10):
            if self.s.sel_logged_in and self._first_visible([self.s.sel_logged_in]) is not None:
                return True
            if self._password_visible():
                return False
            if self.page.locator("tr.mat-mdc-row, tr.mat-row, table tbody tr").count():
                return True
            self.page.wait_for_timeout(500)
        return not self._password_visible()

    # ---- login (browser)
    def _goto(self, url: str, label: str) -> None:
        """Open a page. Single-page apps may never go 'network idle' (polling), so only
        wait for the DOM, then give the network a short chance to settle."""
        with_retry(lambda: self.page.goto(url, wait_until="domcontentloaded"), label=label)
        try:   # Finesse (Fuse template) shows a splash screen until the app has started
            self.page.wait_for_function(
                "() => !document.querySelector('fuse-splash-screen') || "
                "document.body.classList.contains('fuse-splash-screen-hidden')", timeout=30000)
        except Exception:  # noqa: BLE001
            pass
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:  # noqa: BLE001
            pass

    def _visible_inputs(self) -> list[dict]:
        """Visible fillable inputs in page order, each with the words that describe it."""
        out = []
        inputs = self.page.locator("input, textarea")
        for i in range(min(inputs.count(), 60)):
            el = inputs.nth(i)
            try:
                if not el.is_visible() or el.is_disabled():
                    continue
                typ = (el.get_attribute("type") or "text").lower()
                if typ in ("hidden", "checkbox", "radio", "submit", "button", "file", "image", "reset"):
                    continue
                attrs = {a: el.get_attribute(a) or "" for a in
                         ("name", "id", "placeholder", "aria-label", "formcontrolname", "autocomplete", "maxlength", "class")}
                label = ""
                if attrs["id"]:
                    lab = self.page.locator(f'label[for="{attrs["id"]}"]')
                    if lab.count():
                        label = lab.first.inner_text()
                if not label:   # Material / custom forms: the label text sits in the field wrapper
                    label = el.evaluate("""e => { const f = e.closest('mat-form-field, .mat-mdc-form-field, .form-group, .field, label, div');
                                                  const l = f && f.querySelector('mat-label, label, .label');
                                                  return l ? l.innerText : ''; }""") or ""
                words = " ".join([attrs["name"], attrs["id"], attrs["placeholder"], attrs["aria-label"],
                                  attrs["formcontrolname"], attrs["autocomplete"], label])
                out.append({"el": el, "type": typ, "words": words, "attrs": attrs, "label": label.strip()})
            except Exception:  # noqa: BLE001
                continue
        return out

    def _classify(self, fields: list[dict], filled: set[str]) -> dict:
        """Decide which visible box is user id / password / PAN."""
        s = self.s
        roles: dict = {}
        for role, sel in (("user", s.sel_user), ("password", s.sel_password), ("pan", s.sel_pan)):
            if sel:
                el = self._first_visible([sel])
                if el is not None:
                    roles[role] = el
        rest = []
        for f in fields:
            if f["type"] == "password":
                roles.setdefault("password", f["el"])
            elif PAN_HINT.search(f["words"]):
                roles.setdefault("pan", f["el"])
            elif USER_HINT.search(f["words"]):
                roles.setdefault("user", f["el"])
            else:
                rest.append(f)
        # Unlabelled boxes: the first is the user id (if not done yet), the next one the PAN.
        for f in rest:
            if "user" not in roles and "user" not in filled:
                roles["user"] = f["el"]
            elif "pan" not in roles and "pan" not in filled:
                roles["pan"] = f["el"]
        return roles

    def _open_login_form(self) -> list[dict]:
        """Wait for the login form; if the page is a landing page, click its Login button."""
        deadline = time.time() + self.s.http_timeout
        clicked = False
        while time.time() < deadline:
            fields = self._visible_inputs()
            if fields:
                return fields
            if not clicked and time.time() > deadline - self.s.http_timeout / 2:
                for b in self.page.get_by_role("button").all()[:20] + self.page.get_by_role("link").all()[:30]:
                    try:
                        if b.is_visible() and re.match(r"^\s*(log\s*-?\s*in|sign\s*-?\s*in|client\s*login)\s*$",
                                                       b.inner_text() or "", re.I):
                            b.click()
                            clicked = True
                            break
                    except Exception:  # noqa: BLE001
                        continue
            self.page.wait_for_timeout(500)
        return []

    def _save_login_debug(self, note: str) -> str:
        """Write what the login page looked like (no typed values) for support."""
        try:
            d = self.s.data_dir
            d.mkdir(parents=True, exist_ok=True)
            lines = [f"Finesse login page — {note}", f"URL: {self.page.url}",
                     f"Frames: {len(self.page.frames)}", "", "Visible input boxes (in page order):"]
            for i, f in enumerate(self._visible_inputs(), 1):
                a = f["attrs"]
                lines.append(f"  {i}. type={f['type']} id={a['id']!r} name={a['name']!r} placeholder={a['placeholder']!r} "
                             f"aria-label={a['aria-label']!r} formcontrolname={a['formcontrolname']!r} label={f['label']!r}")
            btns = []
            for b in self.page.locator("button, input[type=submit], a").all()[:40]:
                try:
                    if b.is_visible():
                        t = (b.inner_text() or b.get_attribute("value") or "").strip()
                        if t:
                            btns.append(t[:40])
                except Exception:  # noqa: BLE001
                    pass
            lines += ["", "Visible buttons / links: " + " | ".join(btns[:30])]
            for fr in self.page.frames[1:]:
                lines.append(f"Inner frame: {fr.url}")
            path = d / "finesse_login_page.txt"
            path.write_text(redact("\n".join(lines)), encoding="utf-8")
            try:
                for f in self._visible_inputs():
                    if f["type"] != "password":
                        f["el"].fill("")          # never put typed ids / PAN in the screenshot
            except Exception:  # noqa: BLE001
                pass
            self.page.screenshot(path=str(d / "finesse_login_page.png"), full_page=True)
            return str(path)
        except Exception:  # noqa: BLE001
            return ""

    def _layout_error(self, what: str) -> LoginError:
        path = self._save_login_debug(what)
        hint = (f" Details saved to {path} (and a screenshot next to it) — send that file to support."
                if path else "")
        return LoginError(f"Finesse login page layout changed: {what}.{hint}")

    def login(self) -> None:
        s = self.s
        self._goto(s.login_url, "Open Finesse login page")
        if not self._open_login_form():
            raise self._layout_error("no input boxes appeared on the login page")
        values = {"user": s.finesse_user_id, "password": s.finesse_password, "pan": s.finesse_pan}
        filled: set[str] = set()
        # Fill whatever the current screen asks for, submit, repeat (User ID / Password / PAN
        # may be on one screen or spread over two or three).
        for _ in range(4):
            roles = self._classify(self._visible_inputs(), filled)
            todo = {k: v for k, v in roles.items() if k not in filled}
            if not todo:
                break
            for role, el in todo.items():
                el.fill(values[role])
                filled.add(role)
            self._submit()
            err = self._visible_error()
            if err:
                raise LoginError(f"Finesse rejected the login: {err}")
            # give the next screen (or the app) time to appear
            deadline = time.time() + min(s.http_timeout, 20)
            while time.time() < deadline:
                self.page.wait_for_timeout(500)
                if self._visible_error() or not self._visible_inputs():
                    break
                if {k for k in self._classify(self._visible_inputs(), filled)} - filled:
                    break
        err = self._visible_error()
        if err:
            raise LoginError(f"Finesse rejected the login: {err}")
        if "user" not in filled or "password" not in filled:
            raise self._layout_error("could not find the " + ("user id" if "user" not in filled else "password") + " box")
        if s.sel_logged_in:
            if self._first_visible([s.sel_logged_in]) is None:
                raise LoginError("Finesse login did not complete (logged-in marker FINESSE_SEL_LOGGED_IN not found).")
        elif self._password_visible():
            raise LoginError("Finesse login failed — still on the login page. Check the Finesse User ID, Password and PAN "
                             "(run SETUP.bat again to correct them).")

    # ---- Client Master Report (Reports > Corporate Reports > Other Reports > report > Generate)
    def _click_text(self, text: str, wait_s: float = 15) -> bool:
        """Click the visible element whose text is ``text`` (exact first, then contains),
        preferring links, buttons, menu items and panel headers."""
        deadline = time.time() + wait_s
        while time.time() < deadline:
            for exact in (True, False):
                loc = self.page.get_by_text(text, exact=exact)
                for i in range(min(loc.count(), 15)):
                    el = loc.nth(i)
                    try:
                        if not el.is_visible():
                            continue
                        target = el.locator(f"xpath=ancestor-or-self::*[self::a or self::button or @role='menuitem' "
                                            f"or @role='tab' or @role='button' or self::mat-expansion-panel-header][1]")
                        (target.first if target.count() else el).click()
                        self.page.wait_for_timeout(800)
                        return True
                    except Exception:  # noqa: BLE001
                        continue
            self.page.wait_for_timeout(500)
        return False

    def _choose_report(self, name: str) -> bool:
        """Pick ``name`` in the report dropdown (native <select>, mat-select or a plain list)."""
        want = name.strip().lower()
        for i in range(self.page.locator("select").count()):
            sel = self.page.locator("select").nth(i)
            try:
                if not sel.is_visible():
                    continue
                for o in sel.locator("option").all():
                    if want in (o.inner_text() or "").strip().lower():
                        sel.select_option(label=o.inner_text().strip())
                        self.page.wait_for_timeout(800)
                        return True
            except Exception:  # noqa: BLE001
                continue
        triggers = self.page.locator("mat-select, [role=combobox], .mat-mdc-select, ng-select, .dropdown-toggle")
        for i in range(min(triggers.count(), 10)):
            t = triggers.nth(i)
            try:
                if not t.is_visible():
                    continue
                t.click()
                self.page.wait_for_timeout(600)
                opts = self.page.locator("mat-option, [role=option], .ng-option, .dropdown-item")
                for j in range(min(opts.count(), 200)):
                    o = opts.nth(j)
                    if want in (o.inner_text() or "").strip().lower():
                        o.scroll_into_view_if_needed()
                        o.click()
                        self.page.wait_for_timeout(800)
                        return True
                self.page.keyboard.press("Escape")
            except Exception:  # noqa: BLE001
                continue
        return self._click_text(name, wait_s=3)     # a plain list of report names

    def _save_page_debug(self, stem: str, note: str) -> str:
        try:
            d = self.s.data_dir
            d.mkdir(parents=True, exist_ok=True)
            texts = []
            for el in self.page.locator("a, button, [role=menuitem], [role=tab], mat-expansion-panel-header, "
                                        "mat-select, select, mat-option, [role=option]").all()[:120]:
                try:
                    if el.is_visible():
                        t = re.sub(r"\s+", " ", el.inner_text() or "").strip()
                        if t:
                            texts.append(t[:50])
                except Exception:  # noqa: BLE001
                    pass
            path = d / f"{stem}.txt"
            path.write_text(redact(f"{note}\nURL: {self.page.url}\n\nVisible menus / buttons / options:\n  "
                                   + "\n  ".join(texts)), encoding="utf-8")
            self.page.screenshot(path=str(d / f"{stem}.png"), full_page=True)
            return str(path)
        except Exception:  # noqa: BLE001
            return ""

    def _report_error(self, what: str) -> LayoutChangedError:
        path = self._save_page_debug("finesse_report_page", what)
        return LayoutChangedError(what + (f" Details saved to {path} (and a screenshot next to it)." if path else ""))

    def download_client_master_report(self) -> list[ClientRecord]:
        s = self.s
        self._goto(s.base_url, "Open Finesse")
        for item in s.finesse_report_path:
            if not self._click_text(item):
                raise self._report_error(f"Report menu item {item!r} not found (FINESSE_REPORT_PATH).")
        if not self._choose_report(s.finesse_report_name):
            raise self._report_error(f"Report {s.finesse_report_name!r} not found in the report list (FINESSE_REPORT_NAME).")
        self.downloads.clear()
        if not self._click_text(s.finesse_report_button):
            raise self._report_error(f"Button {s.finesse_report_button!r} not found (FINESSE_REPORT_BUTTON).")
        log.info("Generating the %s…", s.finesse_report_name)
        deadline = time.time() + s.finesse_report_timeout
        nudged = False
        while not self.downloads and time.time() < deadline:
            self.page.wait_for_timeout(1000)
            # some reports show a Download / Export button once ready
            if not nudged and time.time() > deadline - s.finesse_report_timeout + 15:
                for t in ("Download", "Download Excel", "Export to Excel", "Export"):
                    if self._click_text(t, wait_s=0.5):
                        nudged = True
                        break
        if not self.downloads:
            try:            # no file: the report may have been shown on screen instead
                head, rows = self._pick_table()
                return from_dicts([dict(zip(head, r)) for r in rows])
            except LayoutChangedError:
                pass
            raise self._report_error(f"Finesse did not download the {s.finesse_report_name} within "
                                     f"{s.finesse_report_timeout}s (FINESSE_REPORT_TIMEOUT_SECONDS).")
        dl = self.downloads[-1]
        path = dl.path()
        if not path:
            raise FinesseError(f"The report download failed: {dl.failure() or 'unknown error'}")
        data = Path(path).read_bytes()
        name = dl.suggested_filename or "report"
        log.info("Report downloaded (%s, %d KB) — reading it", name, len(data) // 1024)
        try:
            return from_file_bytes(data, name)
        finally:
            try:
                dl.delete()          # the file holds PANs: don't leave it on disk
            except Exception:  # noqa: BLE001
                pass

    # ---- finding the client list page
    COMMON_CLIENT_ROUTES = ("#/clients", "#/client", "#/client-list", "#/clients/list", "#/client-master",
                            "#/masters/clients", "#/masters/client-master", "#/admin/clients", "#/backoffice/clients")

    def _cache_file(self):
        return self.s.data_dir / "client_list_url.txt"

    def _has_client_table(self, wait_s: float = 12) -> bool:
        deadline = time.time() + wait_s
        while time.time() < deadline:
            try:
                self._pick_table()
                return True
            except LayoutChangedError:
                pass
            if self._password_visible():
                return False
            self.page.wait_for_timeout(500)
        return False

    def _try_menu_path(self) -> bool:
        for item in self.s.finesse_menu_path:
            loc = self.page.get_by_text(item, exact=True)
            if loc.count() == 0:
                loc = self.page.get_by_text(item)
            target = next((loc.nth(i) for i in range(min(loc.count(), 10)) if loc.nth(i).is_visible()), None)
            if target is None:
                return False
            target.click()
            self.page.wait_for_timeout(800)
        return self._has_client_table()

    def _client_link_candidates(self) -> list[str]:
        """Menu / page links that look like they lead to the client list, best first."""
        links = self.page.evaluate("""() => [...document.querySelectorAll('a[href], [routerlink], [ng-reflect-router-link]')]
            .map(a => ({text: (a.innerText || a.getAttribute('title') || '').replace(/\\s+/g, ' ').trim(),
                        href: a.getAttribute('href') || a.getAttribute('routerlink') || a.getAttribute('ng-reflect-router-link') || ''}))""")
        scored = []
        for l in links:
            href, text = l["href"].strip(), l["text"].lower()
            if not href or href.startswith(("mailto:", "tel:", "javascript:")) or "/profile/" in href:
                continue
            if not re.search(r"client", text + " " + href, re.I):
                continue
            score = 0
            if re.fullmatch(r"(all\s+)?clients?(\s+(list|master|management))?", text):
                score += 10
            if re.search(r"/clients?(/list)?/?$|client-?(list|master)", href, re.I):
                score += 5
            if re.search(r"report|ledger|holding|transaction|add|new|create|edit|login|history", text + href, re.I):
                score -= 6
            scored.append((score, href))
        out = []
        for _, h in sorted(scored, key=lambda x: -x[0]):
            if h.startswith("/") and not h.startswith("#"):
                h = urljoin(self.s.base_url, h)
            elif not h.startswith(("http", "#")):
                h = "#/" + h.lstrip("/")
            if h not in out:
                out.append(h)
        return out

    def open_client_list(self) -> None:
        """Open the page with the client table: the configured address, else the one found
        last time, else the menu path, else search the menu links and common addresses."""
        s = self.s
        if s.finesse_client_list_url:
            self._goto(urljoin(s.base_url, s.finesse_client_list_url), "Open client list")
            if self._password_visible() and not self.page.locator("table").count():
                raise SessionExpired("Finesse showed the login page when opening the client list")
            return
        cache = self._cache_file()
        tried: list[str] = []
        if cache.exists():
            url = cache.read_text(encoding="utf-8").strip()
            if url:
                self._goto(url, "Open client list")
                if self._has_client_table():
                    return
                tried.append(url)
        if self._try_menu_path():
            self._remember(self.page.url)
            return
        candidates = self._client_link_candidates() + [r for r in self.COMMON_CLIENT_ROUTES]
        for c in candidates:
            url = urljoin(s.base_url, c)
            if url in tried:
                continue
            tried.append(url)
            log.info("Looking for the client list at %s", url)
            self._goto(url, "Open client list")
            if self._has_client_table(wait_s=10):
                self._remember(self.page.url)
                return
            if self._password_visible():
                raise SessionExpired("Finesse showed the login page while looking for the client list")
        found = ", ".join(c for c in candidates[:8]) or "none"
        raise LayoutChangedError(
            "Could not find the Finesse client list page automatically (client links tried: " + found + "). "
            "Open the client list in Finesse, copy the address from the browser's address bar and put it in .env as "
            "FINESSE_CLIENT_LIST_URL=<address>.")

    def _remember(self, url: str) -> None:
        try:
            self._cache_file().parent.mkdir(parents=True, exist_ok=True)
            self._cache_file().write_text(url, encoding="utf-8")
            log.info("Client list found at %s (remembered for next time)", url)
        except OSError:
            pass

    def _maximize_page_size(self) -> None:
        """Pick the largest 'rows per page' option if the grid has one."""
        if self._maximize_material_page_size():
            return
        try:
            selects = self.page.locator("select")
            for i in range(min(selects.count(), 10)):
                sel = selects.nth(i)
                if not sel.is_visible():
                    continue
                opts = sel.locator("option").all_inner_texts()
                nums = [(o, int(o)) for o in (x.strip() for x in opts) if o.isdigit()]
                alls = [o for o in opts if o.strip().lower() == "all"]
                if alls:
                    sel.select_option(label=alls[0])
                elif len(nums) >= 2:
                    sel.select_option(label=max(nums, key=lambda x: x[1])[0])
                else:
                    continue
                self.page.wait_for_load_state("networkidle", timeout=self.s.http_timeout * 1000)
                return
        except Exception:  # noqa: BLE001
            pass

    def _maximize_material_page_size(self) -> bool:
        """Angular Material paginator: open its page-size dropdown and pick the largest."""
        try:
            trig = self._first_visible([".mat-mdc-paginator-page-size-select", ".mat-paginator-page-size-select",
                                        "mat-paginator mat-select"])
            if trig is None:
                return False
            trig.click()
            opts = self.page.locator("mat-option, .mat-mdc-option")
            opts.first.wait_for(timeout=5000)
            best, best_n = None, -1
            for i in range(min(opts.count(), 20)):
                t = (opts.nth(i).inner_text() or "").strip()
                n = 10 ** 9 if t.lower() == "all" else int(t) if t.isdigit() else -1
                if n > best_n:
                    best, best_n = opts.nth(i), n
            if best is None:
                self.page.keyboard.press("Escape")
                return False
            if (trig.inner_text() or "").strip() == (best.inner_text() or "").strip():
                self.page.keyboard.press("Escape")       # already showing the most rows
                return True
            before = self._row_count()
            best.click()
            # the grid redraws a moment later: wait for the row count to change (or give up)
            deadline = time.time() + min(self.s.http_timeout, 15)
            while time.time() < deadline and self._row_count() == before:
                self.page.wait_for_timeout(250)
            try:
                self.page.wait_for_load_state("networkidle", timeout=self.s.http_timeout * 1000)
            except Exception:  # noqa: BLE001
                pass
            return True
        except Exception:  # noqa: BLE001
            return False

    def _read_tables(self) -> list[dict]:
        """Every table on the page as {head, rows}. Angular Material tables (Finesse) are
        read by their stable column classes (cdk-column-clientName …), not header text.
        A cell holding a link is read from the link only, so badges beside a name
        ("Joint", "Proprietorship") are left out."""
        return self.page.evaluate("""(sel) => {
          const colOf = c => { const m = (c.className || '').match(/(?:^|\\s)cdk-column-([\\w-]+)/); return m ? m[1] : ''; };
          const txt = c => { const a = c.querySelector('a'); return ((a ? a.innerText : c.innerText) || '').replace(/\\s+/g, ' ').trim(); };
          const tables = sel ? [...document.querySelectorAll(sel)] : [...document.querySelectorAll('table, mat-table, [role=table]')];
          return tables.map(t => {
            let rows = [...t.querySelectorAll('tbody tr, mat-row, [role=row]')].filter(r => !r.querySelector('th, mat-header-cell'));
            if (!rows.length) rows = [...t.querySelectorAll('tr')];
            const first = rows[0] ? [...rows[0].children] : [];
            if (first.length && first.every(c => colOf(c))) {
              const head = first.map(colOf);
              return { head, rows: rows.map(r => head.map(k => { const c = r.querySelector('.cdk-column-' + k); return c ? txt(c) : ''; }))
                                        .filter(r => r.some(x => x)) };
            }
            let head = [...t.querySelectorAll('thead th, thead td')].map(c => c.innerText.trim());
            if (!head.length && rows.length){ head = [...rows[0].children].map(c => c.innerText.trim()); rows = rows.slice(1); }
            return { head, rows: rows.map(r => [...r.children].map(txt)).filter(r => r.some(x => x)) };
          });
        }""", self.s.sel_table or "")

    def _row_count(self) -> int:
        try:
            return max((len(t["rows"]) for t in self._read_tables()), default=0)
        except Exception:  # noqa: BLE001
            return 0

    def _wait_for_rows(self) -> None:
        """Single-page apps draw the table after the page has 'loaded' — wait for real rows."""
        try:
            self.page.wait_for_selector(self.s.sel_table + " tbody tr" if self.s.sel_table else
                                        "tr.mat-mdc-row, tr.mat-row, mat-row, table tbody tr",
                                        timeout=self.s.http_timeout * 1000)
        except Exception:  # noqa: BLE001
            pass

    def _pick_table(self) -> tuple[list[str], list[list[str]]]:
        for t in sorted(self._read_tables(), key=lambda t: -len(t["rows"])):
            try:
                detect_columns(t["head"])
                return t["head"], t["rows"]
            except LayoutChangedError:
                continue
        raise LayoutChangedError("No client table with Client Name / PAN / Trading Account columns found on the Finesse "
                                 "client-list page (layout changed?). Set FINESSE_SEL_TABLE or FINESSE_FIELD_* in .env.")

    def _next_button(self):
        if self.s.sel_next_page:
            return self._first_visible([self.s.sel_next_page])
        mat = self._first_visible([".mat-mdc-paginator-navigation-next", ".mat-paginator-navigation-next",
                                   "button[aria-label='Next page']"])
        if mat is not None:
            return mat
        cands = self.page.locator("button, a, li, span[role=button]")
        for i in range(min(cands.count(), 300)):
            el = cands.nth(i)
            try:
                txt = (el.inner_text() or "").strip()
                aria = (el.get_attribute("aria-label") or "") + " " + (el.get_attribute("title") or "")
                if (NEXT_TEXT.match(txt) or re.search(r"\bnext\b", aria, re.I)) and el.is_visible():
                    return el
            except Exception:  # noqa: BLE001
                continue
        return None

    @staticmethod
    def _disabled(el) -> bool:
        try:
            cls = (el.get_attribute("class") or "").lower()
            return el.is_disabled() or "disabled" in cls or (el.get_attribute("aria-disabled") or "") == "true"
        except Exception:  # noqa: BLE001
            return True

    def _walk_pages(self, on_page: Callable[[list[str], list[list[str]]], bool]) -> None:
        """Open the client grid and call ``on_page(head, rows)`` for every page (stop on True)."""
        self.open_client_list()
        self._wait_for_rows()
        self._maximize_page_size()
        head, rows = self._pick_table()
        if on_page(head, rows):
            return
        prev = rows[:1]
        for _ in range(2000):
            nxt = self._next_button()
            if nxt is None or self._disabled(nxt):
                break
            nxt.click()
            # The grid may page in the browser (no network): wait until the first row changes.
            deadline = time.time() + self.s.http_timeout
            while True:
                try:
                    self.page.wait_for_load_state("networkidle", timeout=2000)
                except Exception:  # noqa: BLE001
                    pass
                _, rows = self._pick_table()
                if (rows and rows[:1] != prev) or time.time() > deadline:
                    break
                self.page.wait_for_timeout(300)
            if not rows or rows[:1] == prev:
                break
            prev = rows[:1]
            if on_page(head, rows):
                return

    def read_client_list(self) -> list[ClientRecord]:
        all_rows: list[list[str]] = []
        head_box: list[list[str]] = []

        def take(head, rows):
            head_box[:] = [head]
            all_rows.extend(rows)
            self._note_profile_links()
            return False

        self._walk_pages(take)
        return from_dicts([dict(zip(head_box[0], r)) for r in all_rows]) if head_box else []

    # ---- trading account from the client's profile (Portfolios > "Trading Account : HK1234")
    def _note_profile_links(self) -> None:
        """Remember client code -> profile link for the rows on screen."""
        if not hasattr(self, "profile_links"):
            self.profile_links = {}
        try:
            pairs = self.page.evaluate("""() => [...document.querySelectorAll('tr, mat-row, [role=row]')].map(r => {
                const a = r.querySelector('a[href*="profile"]');
                const c = r.querySelector('.cdk-column-clientCode') || r.querySelector('td');
                return a && c ? [c.innerText.replace(/\\s+/g, ' ').trim(), a.getAttribute('href')] : null; }).filter(Boolean)""")
            for code, href in pairs:
                if code and href:
                    self.profile_links[code.upper()] = href
        except Exception:  # noqa: BLE001
            pass

    def collect_profile_links(self, wanted: set[str]) -> dict[str, str]:
        if not hasattr(self, "profile_links"):
            self.profile_links = {}

        def take(_head, _rows):
            self._note_profile_links()
            return wanted.issubset(self.profile_links)

        if not wanted.issubset(self.profile_links):
            self._walk_pages(take)
        return {c: self.profile_links[c] for c in wanted if c in self.profile_links}

    TRADING_ACCT_RE = re.compile(r"Trading\s*A(?:ccount|/c)\s*(?:No\.?|Number)?\s*:?\s*((?=[A-Za-z0-9\-/]*\d)[A-Za-z0-9][A-Za-z0-9\-/]{1,24})", re.I)

    def trading_accounts_from_profiles(self, codes: list[str]) -> dict[str, list[str]]:
        """Open each client's profile and read every 'Trading Account : X' under Portfolios.
        Returns {code: [accounts]} for the profiles that were read ([] = none shown)."""
        links = self.collect_profile_links(set(codes))
        out: dict[str, list[str]] = {}
        for code in codes:
            href = links.get(code)
            if not href:
                continue
            try:
                self._goto(urljoin(self.s.base_url, href), "Open client profile")
                text, deadline = "", time.time() + min(self.s.http_timeout, 25)
                while time.time() < deadline:      # wait for THIS client's page, with its portfolios
                    text = self.page.inner_text("body")
                    if code in text.upper() and re.search(r"trading\s*a(ccount|/c)", text, re.I):
                        break
                    self.page.wait_for_timeout(500)
                if code not in text.upper():
                    continue
                accts = []
                for m in self.TRADING_ACCT_RE.finditer(text):
                    a = m.group(1).upper()
                    if a not in accts:
                        accts.append(a)
                out[code] = accts
            except FinesseError:
                raise
            except Exception as e:  # noqa: BLE001 — one bad profile must not stop the rest
                log.warning("Could not read the profile of %s: %s", code, type(e).__name__)
        return out
