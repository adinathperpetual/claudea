import logging

import pytest

from finesse_sync import db, master
from finesse_sync.finesse import NetworkError, with_retry
from finesse_sync.names import best_matches, name_from_file, score_names
from finesse_sync.records import ClientRecord, LayoutChangedError, find_records_in_json, from_dicts, from_file_bytes
from finesse_sync.security import Cipher, is_valid_pan, mask_pan
from finesse_sync.sync import run_sync

RECS = [
    ClientRecord("T001", "GAYATRI JAIN", "abcpj1234k"),
    ClientRecord("T002", "SOHAIL FAKHRUDDIN PATEL", "BCDPP2345L"),
    ClientRecord("T003", "MAHAVIR JALAMCHAND OSWAL HUF", "CDEHO3456M"),
]


def fetch(recs):
    return lambda: (list(recs), "test")


def test_pan_rules():
    assert is_valid_pan("ABCDE1234F") and is_valid_pan(" abcde1234f ")
    assert not is_valid_pan("ABCD1234F") and not is_valid_pan("ABCDE12345")
    assert mask_pan("ABCDE1234F") == "ABCDE****F"
    assert "1234" not in mask_pan("12345")


def test_encryption_at_rest():
    run_sync("test", fetch(RECS))
    with db.connect() as c:
        row = db.get_client(c, "T001")
    assert "ABCPJ1234K" not in row["pan_enc"]
    assert Cipher().decrypt(row["pan_enc"]) == "ABCPJ1234K"   # normalised to uppercase
    raw = db.db_path().read_bytes()
    assert b"ABCPJ1234K" not in raw


def test_upsert_update_flag_reactivate():
    r = run_sync("test", fetch(RECS))
    assert (r["status"], r["added"], r["fetched"]) == ("success", 3, 3)
    changed = [ClientRecord("T001", "GAYATRI R JAIN", "ABCPJ1234K"), RECS[1], RECS[2],
               ClientRecord("T004", "NEW CLIENT", "ZZZPZ9999Z")]
    r = run_sync("test", fetch(changed))
    assert (r["added"], r["updated"], r["unchanged"]) == (1, 1, 2)
    r = run_sync("test", fetch(changed[:1] + changed[1:3] + [ClientRecord("T005", "X Y", "XXXPX1111X")]))
    assert r["flagged_missing"] == 1          # T004 gone: flagged, not deleted
    with db.connect() as c:
        t4 = db.get_client(c, "T004")
    assert t4 is not None and t4["active"] == 0 and t4["missing_since"]
    r = run_sync("test", fetch(changed + [ClientRecord("T005", "X Y", "XXXPX1111X")]))
    assert r["reactivated"] == 1


def test_invalid_pan_logged_and_bad_rows_skipped():
    r = run_sync("test", fetch(RECS + [ClientRecord("T009", "BAD PAN", "12345"), ClientRecord("", "NO CODE", "ABCDE1234F")]))
    assert r["status"] == "partial" and r["invalid_pan"] == 1 and r["added"] == 4
    assert any("invalid PAN" in e for e in r["errors"]) and any("missing trading account" in e for e in r["errors"])
    assert not any("12345" in e for e in r["errors"])  # PAN never logged in clear


def test_failed_fetch_keeps_last_data():
    run_sync("test", fetch(RECS))

    def boom():
        from finesse_sync.finesse import FinesseError
        raise FinesseError("Finesse down")
    r = run_sync("test", boom)
    assert r["status"] == "failed" and "Finesse down" in r["message"]
    assert len(master.directory()) == 3


def test_mass_disappearance_guard():
    run_sync("test", fetch(RECS))
    r = run_sync("test", fetch(RECS[:1]))       # 2 of 3 would vanish (> 30%)
    assert r["status"] == "failed" and "aborted" in r["message"]
    assert len(master.directory()) == 3


def test_exceptional_and_resolution():
    run_sync("test", fetch(RECS))
    c = Cipher()
    master.add_exceptional(c, "T002", "sohail@123")
    with pytest.raises(master.MasterError):          # PAN = default rule, not exceptional
        master.add_exceptional(c, "T001", "ABCPJ1234K")
    items = master.list_exceptional(c)
    assert [i["trading_code"] for i in items] == ["T002"]       # ONLY exceptional clients
    assert items[0]["password"] != "sohail@123" and items[0]["pan_masked"] == "BCDPP****L"
    # exceptional first
    r = master.resolve_passwords(c, file_name="CN_2026-10-01_SOHAIL PATEL.pdf")
    assert r["passwords"] == ["sohail@123"] and r["sources"] == ["exceptional"]
    # default rule
    r = master.resolve_passwords(c, file_name="note_gayatri jain.pdf")
    assert r["passwords"] == ["ABCPJ1234K"] and r["sources"] == ["default"]
    # by trading code (explicit and inside the file name)
    assert master.resolve_passwords(c, trading_code="t003")["passwords"] == ["CDEHO3456M"]
    assert master.resolve_passwords(c, file_name="T003_contract.pdf")["passwords"] == ["CDEHO3456M"]
    # HUF never matches an individual
    assert master.resolve_passwords(c, file_name="x_MAHAVIR OSWAL.pdf")["passwords"] == []
    assert master.resolve_passwords(c, file_name="x_MAHAVIR OSWAL HUF.pdf")["passwords"] == ["CDEHO3456M"]


def test_import_exceptional_skips_default_passwords():
    run_sync("test", fetch(RECS))
    rows = [["Client Name", "Trading Account", "Password", "Password 2"],
            ["GAYATRI JAIN", "T001", "ABCPJ1234K", ""],          # = PAN -> not exceptional
            ["SOHAIL PATEL", "T002", "sp2024", "BCDPP2345L"],
            ["UNKNOWN", "T777", "pw777", ""],
            ["NO CODE", "", "zzz", ""]]
    res = master.import_exceptional(Cipher(), rows)
    assert res["added"] == 2 and res["skipped_default"] == 2 and res["no_code"] == 1 and res["unknown_code"] == 1
    statuses = {i["trading_code"]: i["status"] for i in master.list_exceptional(Cipher())}
    assert statuses == {"T002": "active", "T777": "not_in_finesse"}


def test_records_parsing():
    csv = b"Client Master Report\nGenerated today\n\nTrading Account No,Client Name,PAN Number\nT1,A B,ABCDE1234F\n"
    recs = from_file_bytes(csv, "x.csv")
    assert recs == [ClientRecord("T1", "A B", "ABCDE1234F")]
    payload = {"meta": {"n": 1}, "result": {"rows": [{"clientCode": "t1", "clientName": "A", "panNo": "X"}]}}
    rows = find_records_in_json(payload)
    assert from_dicts(rows)[0].trading_code == "T1"
    with pytest.raises(LayoutChangedError):
        from_dicts([{"foo": 1, "bar": 2}])
    with pytest.raises(LayoutChangedError):
        from_file_bytes(b"<html><input type=password></html>", "x.csv")


def test_name_matching_matches_js_rules():
    assert name_from_file("CN_123_gayatri jain.pdf") == "gayatri jain"
    assert score_names("SOHAIL PATEL", "SOHAIL FAKHRUDDIN PATEL") >= 93
    assert score_names("PATEL SOHAIL", "SOHAIL PATEL") == 100
    assert score_names("MAHAVIR OSWAL", "MAHAVIR OSWAL HUF") == 0
    assert best_matches("ravi kumar", ["RAVI KUMAR", "RAVI KUMARI SHAH"]) == ["RAVI KUMAR"]


def test_retry_backoff():
    import httpx
    calls, sleeps = [], []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("down")
        return "ok"
    assert with_retry(flaky, label="t", attempts=4, sleep=sleeps.append) == "ok"
    assert sleeps == [2.0, 4.0]
    with pytest.raises(NetworkError):
        with_retry(lambda: (_ for _ in ()).throw(httpx.ConnectError("down")), label="t", attempts=2, sleep=lambda s: None)


def test_secrets_redacted_from_logs(monkeypatch, caplog):
    monkeypatch.setenv("FINESSE_PASSWORD", "Sup3rSecret!")
    from finesse_sync import config
    config.reset_settings()
    config.install_secret_redaction()
    caplog.handler.addFilter(config._RedactSecrets())
    logging.getLogger("finesse_sync.x").error("login with %s failed", "Sup3rSecret!")
    assert "Sup3rSecret!" not in caplog.text and "***" in caplog.text
