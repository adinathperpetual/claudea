"""Settings for the Client Master Sync service.

Everything comes from environment variables (normally a git-ignored ``.env`` file
next to this project). Credentials are held only in memory and are never logged:
``install_secret_redaction`` scrubs their values from every log record.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


class ConfigError(RuntimeError):
    """A required setting is missing or invalid."""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _env(name)
    try:
        return int(raw) if raw else default
    except ValueError as e:
        raise ConfigError(f"{name} must be a whole number, got {raw!r}") from e


def _bool(name: str, default: bool) -> bool:
    raw = _env(name).lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> list[str]:
    return [p.strip() for p in _env(name, default).split(",") if p.strip()]


@dataclass
class Settings:
    # --- Finesse credentials (required for sync) ---
    finesse_user_id: str = field(default_factory=lambda: _env("FINESSE_USER_ID"))
    finesse_password: str = field(default_factory=lambda: _env("FINESSE_PASSWORD"))
    finesse_pan: str = field(default_factory=lambda: _env("FINESSE_PAN").upper())

    # --- Finesse URLs ---
    finesse_base_url: str = field(default_factory=lambda: _env("FINESSE_BASE_URL", "https://app.perpetualinv.com/finesse"))
    finesse_login_url: str = field(default_factory=lambda: _env("FINESSE_LOGIN_URL"))
    # Fetch method: auto | export | api | playwright
    finesse_fetch_method: str = field(default_factory=lambda: _env("FINESSE_FETCH_METHOD", "auto").lower())
    # 1) report export (CSV / Excel) URL — found via the browser Network tab
    finesse_export_url: str = field(default_factory=lambda: _env("FINESSE_EXPORT_URL"))
    # 2) internal JSON endpoint the client-list page calls
    finesse_api_url: str = field(default_factory=lambda: _env("FINESSE_API_URL"))
    finesse_api_method: str = field(default_factory=lambda: _env("FINESSE_API_METHOD", "GET").upper())
    finesse_api_records_path: str = field(default_factory=lambda: _env("FINESSE_API_RECORDS_PATH"))
    finesse_api_page_param: str = field(default_factory=lambda: _env("FINESSE_API_PAGE_PARAM", "page"))
    finesse_api_size_param: str = field(default_factory=lambda: _env("FINESSE_API_SIZE_PARAM", "pageSize"))
    finesse_api_page_size: int = field(default_factory=lambda: _int("FINESSE_API_PAGE_SIZE", 500))
    finesse_api_first_page: int = field(default_factory=lambda: _int("FINESSE_API_FIRST_PAGE", 1))
    # Optional pure-HTTP login (JSON or form). Empty = log in with Playwright and reuse its cookies.
    finesse_login_api_url: str = field(default_factory=lambda: _env("FINESSE_LOGIN_API_URL"))
    # Field names the login endpoint expects for user id, password, PAN (in that order)
    finesse_login_api_fields: list[str] = field(default_factory=lambda: _list("FINESSE_LOGIN_API_FIELDS", "userId,password,pan"))
    finesse_login_api_format: str = field(default_factory=lambda: _env("FINESSE_LOGIN_API_FORMAT", "json").lower())
    # 3) browser automation fallback
    finesse_client_list_url: str = field(default_factory=lambda: _env("FINESSE_CLIENT_LIST_URL"))
    finesse_menu_path: list[str] = field(default_factory=lambda: [p.strip() for p in _env("FINESSE_MENU_PATH", "Masters > Client Master").split(">") if p.strip()])
    finesse_headless: bool = field(default_factory=lambda: _bool("FINESSE_HEADLESS", True))
    # Optional: use an installed Chrome/Chromium instead of Playwright's bundled one
    finesse_browser_path: str = field(default_factory=lambda: _env("FINESSE_BROWSER_PATH"))

    # CSS selectors (blank = auto-detect). Only needed if auto-detection misses.
    sel_user: str = field(default_factory=lambda: _env("FINESSE_SEL_USER"))
    sel_password: str = field(default_factory=lambda: _env("FINESSE_SEL_PASSWORD"))
    sel_pan: str = field(default_factory=lambda: _env("FINESSE_SEL_PAN"))
    sel_submit: str = field(default_factory=lambda: _env("FINESSE_SEL_SUBMIT"))
    sel_table: str = field(default_factory=lambda: _env("FINESSE_SEL_TABLE"))
    sel_next_page: str = field(default_factory=lambda: _env("FINESSE_SEL_NEXT_PAGE"))
    sel_logged_in: str = field(default_factory=lambda: _env("FINESSE_SEL_LOGGED_IN"))

    # Column / JSON-key names for the three fields (blank = auto-detect by header text)
    field_name: str = field(default_factory=lambda: _env("FINESSE_FIELD_NAME"))
    field_pan: str = field(default_factory=lambda: _env("FINESSE_FIELD_PAN"))
    field_code: str = field(default_factory=lambda: _env("FINESSE_FIELD_TRADING_CODE"))

    # --- Network behaviour ---
    http_timeout: int = field(default_factory=lambda: _int("FINESSE_TIMEOUT_SECONDS", 60))
    retries: int = field(default_factory=lambda: _int("FINESSE_RETRIES", 4))
    session_max_age_minutes: int = field(default_factory=lambda: _int("FINESSE_SESSION_MAX_AGE_MINUTES", 240))

    # --- Sync safety ---
    # Abort (and keep existing data) if Finesse returns fewer clients than this.
    min_records: int = field(default_factory=lambda: _int("SYNC_MIN_RECORDS", 1))
    # Abort if the fetch would flag more than this % of active clients as missing.
    max_missing_pct: int = field(default_factory=lambda: _int("SYNC_MAX_MISSING_PERCENT", 30))

    # --- Schedule ---
    schedule_enabled: bool = field(default_factory=lambda: _bool("SYNC_SCHEDULE_ENABLED", True))
    schedule_cron: str = field(default_factory=lambda: _env("SYNC_SCHEDULE_CRON", "0 8 * * *"))
    timezone: str = field(default_factory=lambda: _env("SYNC_TIMEZONE", "Asia/Kolkata"))

    # --- Storage & security ---
    data_dir: Path = field(default_factory=lambda: Path(_env("CNE_DATA_DIR") or ROOT / "data"))
    db_path: Path = field(default_factory=lambda: Path(_env("CNE_DB_PATH") or (Path(_env("CNE_DATA_DIR") or ROOT / "data") / "client_master.db")))
    encryption_key: str = field(default_factory=lambda: _env("CNE_ENCRYPTION_KEY"))
    admin_tokens: list[str] = field(default_factory=lambda: _list("CNE_ADMIN_TOKENS"))
    extractor_tokens: list[str] = field(default_factory=lambda: _list("CNE_EXTRACTOR_TOKENS"))
    cors_origins: list[str] = field(default_factory=lambda: _list("CNE_CORS_ORIGINS", "null,http://127.0.0.1:8765,http://localhost:8765"))

    @property
    def login_url(self) -> str:
        return self.finesse_login_url or self.finesse_base_url

    def secrets(self) -> list[str]:
        """Values that must never appear in logs or error messages."""
        vals = [self.finesse_password, self.finesse_user_id, self.finesse_pan, self.encryption_key,
                *self.admin_tokens, *self.extractor_tokens]
        return [v for v in vals if v and len(v) >= 3]

    def require_finesse_credentials(self) -> None:
        missing = [n for n, v in (("FINESSE_USER_ID", self.finesse_user_id),
                                  ("FINESSE_PASSWORD", self.finesse_password),
                                  ("FINESSE_PAN", self.finesse_pan)) if not v]
        if missing:
            raise ConfigError("Missing Finesse credentials in .env: " + ", ".join(missing))


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Re-read the environment (used by tests)."""
    global _settings
    _settings = None


class _RedactSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        secrets = get_settings().secrets()
        if not secrets:
            return True
        msg = record.getMessage()
        clean = msg
        for s in secrets:
            clean = clean.replace(s, "***")
        if clean != msg:
            record.msg, record.args = clean, None
        return True


def redact(text: str) -> str:
    for s in get_settings().secrets():
        text = text.replace(s, "***")
    return text


def install_secret_redaction() -> None:
    f = _RedactSecrets()
    root = logging.getLogger()
    for h in root.handlers:
        h.addFilter(f)
    root.addFilter(f)
