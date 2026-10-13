from fastapi.testclient import TestClient

from finesse_sync import api
from finesse_sync.records import ClientRecord
from finesse_sync.sync import run_sync

ADMIN = {"Authorization": "Bearer admin-token-123"}
EXTR = {"Authorization": "Bearer extract-token-456"}


def client():
    run_sync("test", lambda: ([ClientRecord("T1", "GAYATRI JAIN", "ABCPJ1234K", "D0001"),
                               ClientRecord("T2", "RAVI KUMAR", "DEFPK4567N")], "test"))
    return TestClient(api.app)


def test_auth_and_roles():
    with client() as c:
        assert c.get("/api/health").status_code == 200
        assert c.get("/api/exceptional").status_code == 401
        assert c.get("/api/exceptional", headers={"Authorization": "Bearer nope"}).status_code == 401
        assert c.get("/api/exceptional", headers=EXTR).status_code == 403      # extractor can't see the master
        assert c.post("/api/sync", headers=EXTR).status_code == 403
        assert c.get("/api/exceptional", headers=ADMIN).status_code == 200
        assert c.get("/api/directory", headers=EXTR).json()["clients"] == [
            {"trading_code": "T1", "trading_account": "D0001", "name": "GAYATRI JAIN", "active": True}]


def test_exceptional_crud_and_resolve():
    with client() as c:
        r = c.post("/api/exceptional", headers=ADMIN, json={"trading_code": "T2", "password": "ravi99"})
        assert r.status_code == 200
        rid = r.json()["id"]
        assert c.post("/api/exceptional", headers=ADMIN, json={"trading_code": "T1", "password": "ABCPJ1234K"}).status_code == 400
        items = c.get("/api/exceptional", headers=ADMIN).json()["items"]
        assert len(items) == 1 and items[0]["password"] == "r****9" and items[0]["pan_masked"] == "DEFPK****N"
        assert c.get("/api/exceptional?reveal=true", headers=ADMIN).json()["items"][0]["password"] == "ravi99"
        res = c.post("/api/passwords/resolve", headers=EXTR, json={"file_name": "cn_ravi kumar.pdf"}).json()
        assert res["passwords"] == ["ravi99"]
        res = c.post("/api/passwords/resolve", headers=EXTR, json={"file_name": "cn_gayatri jain.pdf"}).json()
        assert res["passwords"] == ["ABCPJ1234K"] and res["sources"] == ["default"]
        assert c.put(f"/api/exceptional/{rid}", headers=ADMIN, json={"password": "ravi100"}).status_code == 200
        assert c.delete(f"/api/exceptional/{rid}", headers=ADMIN).status_code == 200
        assert c.get("/api/exceptional", headers=ADMIN).json()["items"] == []


def test_import_endpoint_and_logs():
    with client() as c:
        csv = b"Client Name,Trading Account,Password\nGAYATRI JAIN,T1,ABCPJ1234K\nRAVI KUMAR,T2,rk1\n"
        r = c.post("/api/exceptional/import", headers=ADMIN, files={"file": ("m.csv", csv, "text/csv")})
        assert r.status_code == 200 and r.json()["added"] == 1 and r.json()["skipped_default"] == 1
        logs = c.get("/api/sync/logs", headers=ADMIN).json()["logs"]
        assert logs and logs[0]["status"] == "success"
        st = c.get("/api/sync/status", headers=ADMIN).json()
        assert st["counts"]["exceptional"] == 1 and st["last_success_at"]


def _locked_pdf(password: str) -> bytes:
    import io

    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(200, 200)
    w.encrypt(password, algorithm="AES-256")
    out = io.BytesIO()
    w.write(out)
    return out.getvalue()


def test_unlock_tries_every_client_pan():
    with client() as c:
        def unlock(data, name):
            return c.post("/api/passwords/unlock", params={"file_name": name}, headers=EXTR,
                          files={"file": (name, data, "application/pdf")})
        # file name names nobody ("FCBL"), but the PDF opens with RAVI KUMAR's PAN
        r = unlock(_locked_pdf("DEFPK4567N"), "09-OCT-2026_FCBL.pdf").json()
        assert r["password"] == "DEFPK4567N" and r["trading_code"] == "T2" and r["source"] == "pan"
        assert r["client_name"] == "RAVI KUMAR"
        # lowercase PAN variant
        assert unlock(_locked_pdf("abcpj1234k"), "x.pdf").json()["source"] == "pan_lowercase"
        # exceptional password of another client
        c.post("/api/exceptional", headers=ADMIN, json={"trading_code": "T1", "password": "gj@2026"})
        r = unlock(_locked_pdf("gj@2026"), "random.pdf").json()
        assert r["password"] == "gj@2026" and r["trading_code"] == "T1" and r["source"] == "exceptional"
        # nothing opens it: no password, but how many clients were checked
        r = unlock(_locked_pdf("nobody-knows"), "a_SURAJ SIPANI.pdf").json()
        assert r["password"] is None and r["client_count"] == 2
        assert unlock(b"not a pdf", "a.pdf").status_code == 400
        assert c.post("/api/passwords/unlock", files={"file": ("a.pdf", b"x")}).status_code == 401
