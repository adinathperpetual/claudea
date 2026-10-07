"""Upload extracted transactions to (a copy of) Finesse and post them from Equity Staging,
using a dummy 'Adinath Chavhan' account. Nothing here talks to the real Finesse."""
import io
import os
import time

import pytest

from finesse_sync import config, poster

from .fake_finesse import PAN, PASSWORD, UNKNOWN_ISIN, USER, FakeFinesse

LOCAL_CHROMIUM = "/opt/pw-browsers/chromium"
HEAD = ["Trading Account", "Client Name", "Trade Date", "Slip Number", "Settlement Number", "NSE Symbol",
        "BSE Scrip Code", "ISIN No", "Trade Type", "Transaction Type", "Quantity", "Market Price Per Share",
        "Brokerage Per Share", "Total STT", "Total GST", "Transaction Category", "Remarks", "Ignore Corporate Action"]


def template(rows) -> bytes:
    from datetime import date
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.append(HEAD)
    for acct, name, d, isin, side, qty, price in rows:
        ws.append([acct, name, date.fromisoformat(d), None, None, None, None, isin, "Delivery", side, qty, price,
                   None, 0, None, "MARKET", "Buy" if side == "PURCHASE" else "Sell", None])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def wait(job_id, *states, timeout=180):
    end = time.time() + timeout
    while time.time() < end:
        j = poster.get_job(job_id)
        if j["status"] in states:
            return j
        time.sleep(0.5)
    raise AssertionError(f"job stuck at {poster.get_job(job_id)}")


@pytest.fixture
def fake(monkeypatch):
    with FakeFinesse() as f:
        monkeypatch.setenv("FINESSE_BASE_URL", f.url)
        monkeypatch.setenv("FINESSE_USER_ID", USER)
        monkeypatch.setenv("FINESSE_PASSWORD", PASSWORD)
        monkeypatch.setenv("FINESSE_PAN", PAN)
        if os.path.exists(LOCAL_CHROMIUM):
            monkeypatch.setenv("FINESSE_BROWSER_PATH", LOCAL_CHROMIUM)
        config.reset_settings()
        yield f


def test_upload_then_confirm_posts_only_the_new_mapped_rows(fake):
    st = fake.state
    # already in staging before our upload
    st["staging"] += [
        {"id": 900, "account": "AC9001", "client": "Adinath Chavhan", "code": "PCA00333", "scrip": "Old Row",
         "date": "01-10-2026", "type": "PURCHASE", "qty": 7, "rate": 50, "mapped": True},
        {"id": 901, "account": "AC9001", "client": "Adinath Chavhan", "code": "PCA00333", "scrip": "Scrip INE222B01022",
         "date": "07-10-2026", "type": "SELL", "qty": 5, "rate": 250.25, "mapped": True},       # same as row 2 below
        {"id": 902, "account": "ZZ1111", "client": "Someone Else", "code": "PCA00999", "scrip": "Other",
         "date": "07-10-2026", "type": "PURCHASE", "qty": 10, "rate": 101.5, "mapped": True},   # same values, other acct
    ]
    st["next_id"] = 1000
    data = template([
        ("AC9001", "Adinath Chavhan", "2026-10-07", "INE111A01011", "PURCHASE", 10, 101.5),     # dummy entry 1
        ("AC9001", "Adinath Chavhan", "2026-10-07", "INE222B01022", "SELL", 5, 250.25),        # dummy 2 (dup)
        ("AC9001", "Adinath Chavhan", "2026-10-07", UNKNOWN_ISIN, "PURCHASE", 3, 99),           # dummy 3 (unmapped)
    ])
    job_id = poster.start(data, "Template-filled.xlsx", "tester")
    j = wait(job_id, "awaiting_confirmation", "failed")
    assert j["status"] == "awaiting_confirmation", j["message"]
    cands = {c["label"].split(" ")[1] + c["label"].split(" ")[2]: c for c in j["data"]["accounts"]["AC9001"]["candidates"]}
    assert {k: c["kind"] for k, c in cands.items()} == {"PURCHASE10": "new", "SELL5": "duplicate", "PURCHASE3": "unmapped"}
    assert st["uploads"] == 1 and st["posted"] == []                  # nothing posted before confirmation
    assert j["data"]["timings"]["upload_total"] > 0

    with pytest.raises(poster.PostError):                             # unmapped rows can't be chosen
        poster.confirm(job_id, [cands["PURCHASE3"]["id"]])
    poster.confirm(job_id)                                            # default: the new rows only
    j = wait(job_id, "posted", "partial", "failed")
    assert j["status"] == "posted", j["message"]
    assert [r["id"] for r in st["posted"]] == [1000]                  # exactly the new mapped row
    left = {r["id"] for r in st["staging"]}
    assert {900, 901, 902, 1001, 1002} <= left                        # old, duplicate, other account, unmapped
    learned = poster.learned("staging_box"), poster.learned("post_confirm")
    assert learned == ("#acct", "Yes")                                # remembered for next time


def test_person_chooses_rows_to_post(fake):
    """Tick a row that was already in staging, untick a new one."""
    st = fake.state
    st["staging"] += [{"id": 901, "account": "AC9001", "client": "Adinath Chavhan", "code": "PCA00333",
                       "scrip": "x", "date": "09-10-2026", "type": "SELL", "qty": 5, "rate": 20, "mapped": True}]
    st["next_id"] = 1000
    data = template([("AC9001", "Adinath Chavhan", "2026-10-09", "INE111A01011", "PURCHASE", 1, 10),   # new, unticked
                     ("AC9001", "Adinath Chavhan", "2026-10-09", "INE222B01022", "SELL", 5, 20),       # dup, ticked
                     ("AC9001", "Adinath Chavhan", "2026-10-09", "INE333C01033", "PURCHASE", 2, 30)])  # new, ticked
    job_id = poster.start(data, "t.xlsx")
    j = wait(job_id, "awaiting_confirmation")
    c = j["data"]["accounts"]["AC9001"]["candidates"]
    assert [x["kind"] for x in c] == ["new", "duplicate", "new"]
    poster.confirm(job_id, [c[1]["id"], c[2]["id"]])
    j = wait(job_id, "posted", "partial", "failed")
    assert j["status"] == "posted", j["message"]
    posted = sorted((r["type"], r["qty"]) for r in st["posted"])
    assert posted == [("PURCHASE", 2), ("SELL", 5)]                   # one SELL 5 (old or new — identical)
    assert sum(1 for r in st["staging"] if r["type"] == "SELL" and r["qty"] == 5) == 1
    assert any(r["type"] == "PURCHASE" and r["qty"] == 1 for r in st["staging"])   # unticked: not posted


def test_cancel_posts_nothing(fake):
    data = template([("AC9001", "Adinath Chavhan", "2026-10-08", "INE111A01011", "PURCHASE", 1, 10)])
    job_id = poster.start(data, "t.xlsx")
    wait(job_id, "awaiting_confirmation")
    poster.cancel(job_id)
    assert poster.get_job(job_id)["status"] == "cancelled" and fake.state["posted"] == []
    with pytest.raises(poster.PostError):
        poster.confirm(job_id)


def test_bad_file_is_refused_before_touching_finesse(fake):
    with pytest.raises(poster.PostError):
        poster.start(template([("", "No Account", "2026-10-08", "INE111A01011", "PURCHASE", 1, 10)]), "t.xlsx")
    assert fake.state.get("uploads", 0) == 0
