# Contract Note Extractor

A Windows tool that unlocks password-protected contract-note PDFs and extracts the trades
(`index.html`), with a small local service (`finesse_sync/`) that:

1. logs in to **Finesse** (`https://app.perpetualinv.com/finesse`) with user ID, password and PAN and
   keeps a **Client Master** (client name, PAN, trading account) — on demand and daily at 08:00 IST;
2. gives the extractor each PDF's password: the client's **PAN in uppercase**, or an
   **exceptional password** entered by the team;
3. can **upload the filled template to Finesse** and post the staging rows after your confirmation.

## Install (Windows)

Run **ContractNoteExtractor-Setup.exe** (from the `latest` release) and click Next → Install.

| What | Where |
|---|---|
| Program | `C:\Program Files\Contract Note Extractor` |
| Settings (`.env`) and data (`data\`: Client Master, logs, tokens) | `C:\ProgramData\Contract Note Extractor` |

- The installer needs admin rights once. It adds Start-menu and desktop shortcuts and can start
  the tool with Windows (keeps the 08:00 sync running).
- The first start asks for the Finesse login, creates the encryption key and access tokens
  (`data\ACCESS_TOKENS.txt` — keep private, back up the key) and opens http://127.0.0.1:8766/.
- Start menu: **Change Finesse login**, **Test Finesse login**, **Sync clients from Finesse now**,
  **Settings and data folder**.
- Updating = running a newer Setup. Settings and data are kept, also on uninstall.
- Finesse is read with the Microsoft Edge (or Google Chrome) already on the PC.

## How a PDF gets its password

1. The client is looked up from the file name (a trading code in it, or the name after the last `_`)
   and that client's exceptional password(s), else PAN in uppercase, are tried.
2. If that does not open it, the service tries every exceptional password and every client's PAN
   (uppercase, then lowercase) against the PDF and returns only the one that opens it.
3. Then the browser's local list, then manual entry. A file that stays locked says why.

The Client Master screen (admin token) lists only exceptional clients.

## Sync behaviour

- Fetch order: Client Master Report download (Reports › Corporate Reports › Other Reports), else
  the on-screen client grid; optional `FINESSE_EXPORT_URL` / `FINESSE_API_URL` first.
- Upsert by client code; clients missing from Finesse are flagged, never deleted.
- Missing trading accounts are read from the client's profile (Portfolios).
- PANs are validated; a sync that returns too few clients or would drop too many is aborted.
- Every run is in the Sync log. Layout problems save `data\finesse_report_page.txt` + screenshot.

## Upload to Finesse

After extraction, an admin clicks **Upload to Finesse**, ticks the rows to upload, waits while the
file is uploaded and the rows are found in Equity Staging, ticks what to post and clicks
**Confirm & Post**. Nothing is posted without that confirmation. Working selectors are remembered
in `data\finesse_learned.json` to make the next run faster.

## Security

- Finesse credentials live only in `.env`; they are scrubbed from every log line.
- PANs, exceptional passwords and the Finesse session are encrypted at rest (Fernet).
- PANs are masked in the UI; the Client Master needs the admin token, password lookups the
  extractor token (given automatically to pages opened on the same PC).
- The service listens on `127.0.0.1` only. Port: `CNE_PORT` in `.env` (default 8766).

All settings are documented in `.env.example`.

## Build the installer

- Automatic: the **Build Windows exe** GitHub Action builds `ContractNoteExtractor-Setup.exe` on every
  push and publishes it on the `latest` release.
- On a Windows PC with Python 3.10+: double-click **BUILD_EXE.bat** → `dist\ContractNoteExtractor-Setup.exe`.
  It uses PyInstaller (`packaging/exe/`) and Inno Setup 6 (`packaging/installer/`), installing Inno Setup
  with winget when missing.

From source (development): `pip install -r requirements.txt`, then `python -m finesse_sync setup` and
`python -m finesse_sync serve --open`.
