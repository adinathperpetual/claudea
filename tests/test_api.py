from fastapi.testclient import TestClient

from finesse_sync import api
from finesse_sync.records import ClientRecord
from finesse_sync.sync import run_sync

ADMIN = {"Authorization": "Bearer admin-token-123"}
EXTR = {"Authorization": "Bearer extract-token-456"}


def client():
    run_sync("test", lambda: ([ClientRecord("T1", "GAYATRI JAIN", "ABCPJ1234K"),
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
        assert c.get("/api/directory", headers=EXTR).json()["clients"][0]["name"] == "GAYATRI JAIN"


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
