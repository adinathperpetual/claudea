# Contract Note Extractor + Client Master Sync

`index.html` is the Contract Note Extractor. It runs entirely in the browser:
it unlocks password-protected contract-note PDFs and extracts the trades.

`finesse_sync/` is the **Client Master Sync** service, a small Python program.
It runs on one office PC or server and does three jobs:

1. It logs in to **Finesse** (`https://app.perpetualinv.com/finesse`) with user ID, password and PAN.
   It fetches every client's **Client Name, PAN and Trading Account**.
2. It keeps a local **Client Master** (SQLite, with PANs encrypted) up to date.
   You can sync on demand with the **Sync from Finesse** button, and it also syncs every day at 08:00 IST.
3. It stores the **exceptional-password list** and gives the extractor each PDF's password.

## Password rule

| Client | Contract-note password |
|---|---|
| Most clients (default rule) | PAN in uppercase, e.g. `ABCDE1234F`, taken from Finesse |
| Exceptional clients | Whatever is stored in the exceptional list, keyed by trading code |

When the extractor opens a PDF, it works out the client from the file name in this order:

1. A trading code inside the file name.
2. Otherwise, the name after the last `_`, e.g. `..._GAYATRI JAIN.pdf`.
   Name matching tolerates missing middle names and reversed name order.

It then tries passwords in this order:

1. The exceptional password(s) for that trading code.
2. If the client has none, the PAN in uppercase.
3. Then the browser's local master (offline fallback).
4. Then manual entry.

The Client Master screen shows **only exceptional clients**.
If you try to add a password that equals the client's PAN, it is rejected because the default rule already covers it.

## Quick start (Windows, no command line)

1. Install Python 3.10+ from python.org and tick **Add python.exe to PATH**.
2. Double-click **SETUP.bat**. It installs everything, then asks for your Finesse user ID, password and PAN.
   It also creates the keys and saves the tokens to `data/ACCESS_TOKENS.txt`.
3. Double-click **START.bat**. The tool opens at http://127.0.0.1:8765/ and is already connected for password lookups.
   An admin pastes the admin token in card 2 to manage the Client Master.
4. Optional: run **AUTOSTART_ON.bat** so the tool starts with Windows and the 08:00 sync always runs.
   Use **SYNC_NOW.bat** and **TEST_LOGIN.bat** for a manual sync and a login check.

Mac/Linux: run `./setup.sh`, then `./start.sh`. The manual steps below do the same thing by hand.

## Setup by hand (Windows or Linux, Python 3.10+)

```bash
python -m venv .venv
.venv\Scripts\activate            # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
playwright install chromium       # or set FINESSE_BROWSER_PATH to an installed Chrome

copy .env.example .env            # Linux/macOS: cp .env.example .env
python -m finesse_sync genkey     # -> paste into CNE_ENCRYPTION_KEY
python -m finesse_sync gentoken   # -> paste into CNE_ADMIN_TOKENS
python -m finesse_sync gentoken   # -> paste into CNE_EXTRACTOR_TOKENS
```

Fill in `FINESSE_USER_ID`, `FINESSE_PASSWORD` and `FINESSE_PAN` in `.env`.
`.env` and `data/` are git-ignored. Credentials are read only from the environment and are scrubbed from every log line.

> **Back up `CNE_ENCRYPTION_KEY`.** Without it, the stored PANs and exceptional passwords cannot be decrypted.

### 1. Check the login

```bash
python -m finesse_sync test-login
```

On a wrong ID, password or PAN you get a clear "Finesse rejected the login: …" message.
If the login fields can't be found, the message says to set `FINESSE_SEL_*` in `.env`.

### 2. Choose the fetch method

Methods in order of preference:

1. **Export:** if Finesse has a CSV/Excel export of the client list, put its URL in `FINESSE_EXPORT_URL`.
2. **Internal JSON API:** run
   ```bash
   python -m finesse_sync discover --show
   ```
   It logs in, opens the client list and records every network call the page makes in `data/discovery.json`.
   Credentials are redacted. Calls that look like a client list, and any export/download buttons, are printed.
   Put the endpoint in `FINESSE_API_URL`, and set the paging params if needed.
3. **Browser automation (default fallback):** Playwright logs in and opens the client list.
   - It uses `FINESSE_CLIENT_LIST_URL`, or clicks through `FINESSE_MENU_PATH` (default `Masters > Client Master`).
   - It picks the largest "rows per page" option and pages through the table.

Columns are detected from their headers, e.g. "Client Name", "PAN No" and "Trading Account No" / "Client Code".
If Finesse uses different labels, set `FINESSE_FIELD_NAME`, `FINESSE_FIELD_PAN` and `FINESSE_FIELD_TRADING_CODE`.

### 3. Run

```bash
python -m finesse_sync sync       # one sync now; prints the result
python -m finesse_sync serve      # API + daily schedule, http://127.0.0.1:8765
```

Open **http://127.0.0.1:8765/** (the service serves the extractor) and sign in as an admin.
Open **2 · Client master** and enter:

- **Extractor token:** saved in this browser so every user can look up passwords.
- **Admin token:** kept for this session only and forgotten on log-out.

Then use **Sync from Finesse**, add or change exceptional passwords, or **Import exceptional passwords**.
The import takes your existing `Client Name | Trading Account | Password` file.
Rows whose password is just the PAN are skipped automatically.

To use the service from other PCs, run `serve --host 0.0.0.0` behind your firewall or reverse proxy (HTTPS).
Add those origins to `CNE_CORS_ORIGINS`.

Other commands: `python -m finesse_sync status`.
To keep the service running, use Windows Task Scheduler (`serve` at startup) or a systemd unit.
If you would rather schedule `python -m finesse_sync sync` yourself, set `SYNC_SCHEDULE_ENABLED=false`.

## Sync behaviour

- **Upsert by trading code.** New clients are inserted. Name and PAN changes are updated.
  Clients missing from Finesse are **flagged** (`active=0`, `missing_since`), never deleted, and reactivated if they come back.
- **PAN validation.** The format must be 5 letters + 4 digits + 1 letter.
  Invalid PANs are stored but flagged and listed in the sync log. The log never shows a full PAN.
- **Safety checks.** The sync is aborted and the existing data kept if:
  - Finesse returns fewer than `SYNC_MIN_RECORDS` clients, or
  - more than `SYNC_MAX_MISSING_PERCENT` of active clients would disappear (default 30%).

  This usually means the page layout changed.
- **Sync log.** Each run records start and end time, trigger (manual/schedule/cli), method, and counts:
  fetched, added, updated, flagged missing, reactivated, invalid PAN.
  It also stores the error list. You can see it under **Sync log** in the UI or at `/api/sync/logs`.
- **Errors.**
  - Network failures are retried with backoff (2s, 4s, 8s…).
  - An expired session is detected (401/403, a redirect to login, or the login page served instead of data) and the service logs in again automatically.
  - The session (cookies or token) is saved encrypted in `data/finesse_session.enc` and reused until it expires.
  - A failed sync never touches existing data. The extractor keeps using the last synced data, and if the service itself is down it falls back to the local master. Extraction never fails because of a sync problem.

## Security

- PANs, exceptional passwords and the Finesse session are encrypted at rest (Fernet, AES-128 + HMAC).
- The UI shows PANs masked (`ABCDE****F`) and passwords masked unless an admin clicks **Reveal**.
- API access needs a token:
  - **Admin tokens:** Client Master screen, sync and edits.
  - **Extractor tokens:** password lookup and the name→trading-code directory only. They cannot list the master.
- In the extractor, the Client Master card is only shown to admin users.
- The service binds to `127.0.0.1` by default.

## API

| Route | Token | Purpose |
|---|---|---|
| `GET /api/health` | none | liveness |
| `POST /api/passwords/resolve` `{file_name, trading_code?, client_name?}` | extractor/admin | candidate passwords, best first |
| `GET /api/directory` | extractor/admin | client names + trading codes (no PAN) |
| `POST /api/sync` · `GET /api/sync/status` · `GET /api/sync/logs` | admin | sync |
| `GET/POST /api/exceptional`, `PUT/DELETE /api/exceptional/{id}`, `POST /api/exceptional/import` | admin | exceptional list |

## Tests

```bash
pip install -r requirements-dev.txt
pytest -q
```

The tests include a fake Finesse portal with a user/password/PAN login, a paginated client table, a JSON API and a CSV export.
The export, JSON API and Playwright paths, session reuse and expiry, and wrong-password handling are all tested end to end.
