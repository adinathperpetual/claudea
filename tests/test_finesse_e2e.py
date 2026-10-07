"""End-to-end against the fake Finesse portal: login (user id + password + PAN),
session reuse/expiry, and all three fetch methods (export, JSON API, Playwright)."""
import os

import httpx
import pytest

from finesse_sync import config
from finesse_sync.finesse import FinesseClient, LoginError
from finesse_sync.sync import run_sync

from .fake_finesse import CLIENTS, PAN, PASSWORD, USER, FakeFinesse

# Use the sandbox / CI Chromium when present; otherwise Playwright's own download.
LOCAL_CHROMIUM = "/opt/pw-browsers/chromium"


def _pw_available() -> bool:
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False
    return True


pw_available = _pw_available()


@pytest.fixture
def fake(monkeypatch):
    with FakeFinesse() as f:
        monkeypatch.setenv("FINESSE_BASE_URL", f.url)
        monkeypatch.setenv("FINESSE_USER_ID", USER)
        monkeypatch.setenv("FINESSE_PASSWORD", PASSWORD)
        monkeypatch.setenv("FINESSE_PAN", PAN.lower())
        if os.path.exists(LOCAL_CHROMIUM):
            monkeypatch.setenv("FINESSE_BROWSER_PATH", LOCAL_CHROMIUM)
        config.reset_settings()
        yield f


def _codes(recs):
    return sorted(r.trading_code for r in recs)


def test_api_with_http_login_and_relogin(fake, monkeypatch):
    monkeypatch.setenv("FINESSE_LOGIN_API_URL", fake.url + "/api/login")
    monkeypatch.setenv("FINESSE_API_URL", "api/clients")
    monkeypatch.setenv("FINESSE_API_PAGE_SIZE", "3")
    monkeypatch.setenv("FINESSE_FETCH_METHOD", "api")
    config.reset_settings()
    with FinesseClient() as fc:
        recs, how = fc.fetch_clients()
    assert how == "api" and _codes(recs) == sorted(c["Client Code"] for c in CLIENTS)
    assert fake.state["logins"] == 1
    with FinesseClient() as fc:                      # stored session is reused
        fc.fetch_clients()
    assert fake.state["logins"] == 1
    httpx.post(fake.url + "/_expire")                # server-side expiry -> automatic re-login
    with FinesseClient() as fc:
        recs, _ = fc.fetch_clients()
    assert fake.state["logins"] == 2 and len(recs) == len(CLIENTS)


def test_wrong_password_clear_error(fake, monkeypatch):
    monkeypatch.setenv("FINESSE_LOGIN_API_URL", fake.url + "/api/login")
    monkeypatch.setenv("FINESSE_PASSWORD", "wrong")
    monkeypatch.setenv("FINESSE_API_URL", "api/clients")
    config.reset_settings()
    r = run_sync("test")
    assert r["status"] == "failed" and "login failed" in r["message"].lower()
    assert "wrong" not in r["message"]


@pytest.mark.skipif(not pw_available, reason="Chromium for Playwright not installed")
def test_browser_login_then_export(fake, monkeypatch):
    monkeypatch.setenv("FINESSE_EXPORT_URL", "export/clients.csv")
    config.reset_settings()
    r = run_sync("test")
    assert r["method"] == "export" and r["fetched"] == len(CLIENTS)
    assert r["invalid_pan"] == 1 and r["status"] == "partial"


@pytest.mark.skipif(not pw_available, reason="Chromium for Playwright not installed")
def test_playwright_table_fallback_with_pagination_and_menu(fake, monkeypatch):
    monkeypatch.setenv("FINESSE_MENU_PATH", "Client Master")
    config.reset_settings()
    r = run_sync("test")
    assert r["method"] == "playwright" and r["fetched"] == len(CLIENTS) and r["added"] == len(CLIENTS)


@pytest.mark.skipif(not pw_available, reason="Chromium for Playwright not installed")
def test_browser_login_rejected(fake, monkeypatch):
    monkeypatch.setenv("FINESSE_PAN", "ZZZZZ9999Z")
    config.reset_settings()
    with pytest.raises(LoginError) as e:
        FinesseClient().login()
    assert "Invalid credentials" in str(e.value)


@pytest.mark.skipif(not pw_available, reason="Chromium for Playwright not installed")
def test_angular_material_grid_like_real_finesse(fake, monkeypatch):
    """Finesse's real grid: cdk-column-* cells, 'Name (ACCOUNT)', badges beside the name,
    async rendering, a page-size dropdown and a Next button that pages in the browser."""
    from finesse_sync import master
    from finesse_sync.security import Cipher

    from .fake_finesse import MAT_CLIENTS
    monkeypatch.setenv("FINESSE_CLIENT_LIST_URL", "clients-material")
    config.reset_settings()
    r = run_sync("test")
    assert r["status"] == "success", r
    assert r["method"] == "playwright" and r["fetched"] == len(MAT_CLIENTS) == r["added"]
    d = {x["trading_code"]: x for x in master.directory(include_inactive=True)}
    assert d["PCA00001"]["name"] == "Alpha Imports Pvt Ltd" and d["PCA00001"]["trading_account"] == "D000001"
    assert d["PCA00011"]["trading_account"] == "KJ26011"
    assert "PCA00005" not in d                      # no trading account -> not offered for the A/c column
    c = Cipher()
    # badge text is not part of the name; PAN rule works by name, account or code
    assert master.resolve_passwords(c, file_name="CN_0710_Esha Verma.pdf")["passwords"] == ["AAAPV0005E"]
    assert master.resolve_passwords(c, file_name="CN_0710_D000013.pdf")["passwords"] == ["AAAPN0013N"]
    assert master.resolve_passwords(c, trading_code="3000002")["passwords"] == ["AAAFB0002B"]
    # an exceptional password entered against the trading account lands on the client code
    added = master.add_exceptional(c, "dk7004", "deepa@123")
    assert added["trading_code"] == "PCA00004" and added["known_client"]
    assert master.resolve_passwords(c, file_name="x_Deepa Kulkarni.pdf")["passwords"] == ["deepa@123"]


@pytest.mark.skipif(not pw_available, reason="Chromium for Playwright not installed")
def test_spa_multi_step_login(fake, monkeypatch):
    monkeypatch.setenv("FINESSE_LOGIN_URL", fake.url + "/spa")
    config.reset_settings()
    FinesseClient().login()
    assert fake.state["logins"] == 1
    monkeypatch.setenv("FINESSE_PAN", "ZZZZZ9999Z")
    config.reset_settings()
    with pytest.raises(LoginError) as e:
        FinesseClient().login()
    assert "Invalid credentials" in str(e.value)


@pytest.mark.skipif(not pw_available, reason="Chromium for Playwright not installed")
def test_login_layout_error_saves_details(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("FINESSE_LOGIN_URL", fake.url + "/blank")
    monkeypatch.setenv("FINESSE_TIMEOUT_SECONDS", "4")
    config.reset_settings()
    with pytest.raises(LoginError) as e:
        FinesseClient().login()
    assert "finesse_login_page.txt" in str(e.value)
    txt = (tmp_path / "finesse_login_page.txt").read_text()
    assert PASSWORD not in txt and (tmp_path / "finesse_login_page.png").exists()
