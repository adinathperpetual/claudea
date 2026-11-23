"""Fill a throw-away Client Master with SAMPLE data for the SOP screenshots.
Run with CNE_DATA_DIR pointing to an empty test folder (never the real one):
    python docs/sop/seed_sample.py
Names, PANs, client codes and trading accounts below are invented."""
from finesse_sync import db, master
from finesse_sync.security import Cipher

CLIENTS = [  # client code, name, PAN (invented), trading account ("" = not found in Finesse)
    ("RS1001", "RAHUL SHARMA", "ABCPS1234K", "D100101"),
    ("MP1002", "MEERA PATEL HUF", "AAAHM5678L", "D100102"),
    ("AG1003", "ANIL KUMAR GUPTA", "BCDPG4321M", ""),
    ("SR1004", "SUNITA RAO", "CDEPR8765N", "D100104"),
]

db.init_db()
cipher, ts = Cipher(), db.now_iso()
with db.connect() as c:
    for code, name, pan, acct in CLIENTS:
        if not db.get_client(c, code):
            db.insert_client(c, code, name, cipher.encrypt(pan), True, ts, acct)
    log = db.start_log(c, "manual")
    db.finish_log(c, log, status="success", method="report",
                  stats={"fetched": 4, "added": 4, "updated": 0, "unchanged": 0},
                  errors=[], message="4 clients fetched from the Client Master Report")
try:
    master.add_exceptional(cipher, "MP1002", "MEERA@2024", "Broker uses a custom password", "admin")
except master.MasterError:
    pass
print("sample Client Master ready")
