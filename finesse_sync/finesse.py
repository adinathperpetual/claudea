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
from typing import Any, Callable, TypeVar
from urllib.parse import urljoin, urlparse

import httpx

from .config import Settings, get_settings, redact
from .records import ClientRecord, LayoutChangedError, detect_columns, find_records_in_json, from_dicts, from_file_bytes
from .security import Cipher

log = logging.getLogger("finesse_sync.finesse")
T = TypeVar("T")

USER_HINT = re.compile(r"(user|login|client.?id|email|uid|username|userid)", re.I)
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
        if method in ("auto", "playwright"):
            plan.append(("playwright", self._fetch_browser))
        if not plan:
            raise FinesseError(f"FINESSE_FETCH_METHOD={method} but its URL is not configured "
                               "(FINESSE_EXPORT_URL / FINESSE_API_URL).")
        errors = []
        for name, fn in plan:
            try:
                recs = self._with_relogin(fn) if name != "playwright" else fn()
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
        url = urljoin(self.s.finesse_base_url + "/", self.s.finesse_export_url)
        r = self._request("GET", url)
        if r.status_code >= 400:
            raise FinesseError(f"Export download returned HTTP {r.status_code}")
        fname = _filename(r) or urlparse(url).path
        return from_file_bytes(r.content, fname, r.headers.get("content-type", ""))

    def _fetch_api(self) -> list[ClientRecord]:
        url = urljoin(self.s.finesse_base_url + "/", self.s.finesse_api_url)
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

    def _fetch_browser(self) -> list[ClientRecord]:
        if self.session is None:
            self.session = self.store.load()      # reuse saved cookies if still valid
        with BrowserSession(self.s, self.session) as b:
            if not b.is_logged_in():
                b.login()
            recs = b.read_client_list()
            st = b.session_state()
            st.created_at = time.time()
            self.session = st
            self.store.save(st)
            return recs


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
        try:
            with_retry(lambda: self.page.goto(self.s.finesse_client_list_url or self.s.finesse_base_url,
                                              wait_until="networkidle"), label="Open Finesse")
        except FinesseError:
            raise
        if self.s.sel_logged_in:
            return self._first_visible([self.s.sel_logged_in]) is not None
        return not self._password_visible()

    def login(self) -> None:
        s = self.s
        with_retry(lambda: self.page.goto(s.login_url, wait_until="networkidle"), label="Open Finesse login page")
        user = self._first_visible([s.sel_user]) if s.sel_user else self._input_matching(USER_HINT)
        pw = self._first_visible([s.sel_password or "input[type=password]"])
        if user is None or pw is None:
            raise LoginError("Finesse login page layout changed: could not find the "
                             + ("user id" if user is None else "password") + " field. "
                             "Set FINESSE_SEL_USER / FINESSE_SEL_PASSWORD in .env (run `python -m finesse_sync discover` to inspect).")
        pan = self._first_visible([s.sel_pan]) if s.sel_pan else self._input_matching(PAN_HINT)
        if pan is not None:
            try:
                if pan.evaluate("(e, other) => e === other", user.element_handle()):
                    pan = None          # the "user" heuristic and the PAN heuristic hit the same box
            except Exception:  # noqa: BLE001
                pass
        user.fill(s.finesse_user_id)
        pw.fill(s.finesse_password)
        if pan is not None:
            pan.fill(s.finesse_pan)
        self._submit()

        # Two-step login: PAN asked on a second screen.
        if pan is None:
            pan2 = self._first_visible([s.sel_pan]) if s.sel_pan else self._input_matching(PAN_HINT)
            if pan2 is not None:
                pan2.fill(s.finesse_pan)
                self._submit()

        err = self._visible_error()
        if err:
            raise LoginError(f"Finesse rejected the login: {err}")
        if s.sel_logged_in:
            if self._first_visible([s.sel_logged_in]) is None:
                raise LoginError("Finesse login did not complete (logged-in marker FINESSE_SEL_LOGGED_IN not found).")
        elif self._password_visible():
            raise LoginError("Finesse login failed — still on the login page. Check FINESSE_USER_ID, FINESSE_PASSWORD and FINESSE_PAN.")

    def open_client_list(self) -> None:
        s = self.s
        if s.finesse_client_list_url:
            url = urljoin(s.finesse_base_url + "/", s.finesse_client_list_url)
            with_retry(lambda: self.page.goto(url, wait_until="networkidle"), label="Open client list")
        else:
            for item in s.finesse_menu_path:
                loc = self.page.get_by_text(item, exact=True)
                if loc.count() == 0:
                    loc = self.page.get_by_text(item)
                target = None
                for i in range(min(loc.count(), 10)):
                    if loc.nth(i).is_visible():
                        target = loc.nth(i)
                        break
                if target is None:
                    raise LayoutChangedError(f"Finesse menu item {item!r} not found (FINESSE_MENU_PATH). "
                                             "Set FINESSE_CLIENT_LIST_URL to the client list page URL instead.")
                target.click()
                try:
                    self.page.wait_for_load_state("networkidle", timeout=s.http_timeout * 1000)
                except Exception:  # noqa: BLE001
                    pass
        if self._password_visible() and not self.page.locator("table").count():
            raise SessionExpired("Finesse showed the login page when opening the client list")

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

    def read_client_list(self) -> list[ClientRecord]:
        self.open_client_list()
        self._wait_for_rows()
        self._maximize_page_size()
        head, rows = self._pick_table()
        all_rows = list(rows)
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
            all_rows.extend(rows)
        return from_dicts([dict(zip(head, r)) for r in all_rows])