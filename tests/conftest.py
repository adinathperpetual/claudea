import os
import sys
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ENV_KEYS = [k for k in os.environ if k.startswith(("FINESSE_", "CNE_", "SYNC_"))]


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    for k in list(os.environ):
        if k.startswith(("FINESSE_", "CNE_", "SYNC_")):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("CNE_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CNE_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("CNE_ADMIN_TOKENS", "admin-token-123")
    monkeypatch.setenv("CNE_EXTRACTOR_TOKENS", "extract-token-456")
    monkeypatch.setenv("SYNC_SCHEDULE_ENABLED", "false")
    monkeypatch.setenv("FINESSE_RETRIES", "2")
    monkeypatch.setenv("FINESSE_TIMEOUT_SECONDS", "15")
    from finesse_sync import config, db
    config.reset_settings()
    db.init_db()
    yield
    config.reset_settings()
