"""Command line:

  python -m finesse_sync setup        first-time setup wizard (Finesse login, keys, tokens)
  python -m finesse_sync serve        start the API + scheduler (and serve the extractor UI)
  python -m finesse_sync sync         run one sync now (for cron / Task Scheduler)
  python -m finesse_sync test-login   log in to Finesse and report success / failure
  python -m finesse_sync discover     log in, open the client list, record the network calls
                                      the page makes (to find an export / JSON endpoint)
  python -m finesse_sync status       last sync + counts
  python -m finesse_sync genkey       new CNE_ENCRYPTION_KEY
  python -m finesse_sync gentoken     new random access token
"""
from __future__ import annotations

import argparse
import json
import logging
import secrets
import sys


def _setup_logging(verbose: bool = False) -> None:
    from .config import install_secret_redaction

    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # httpx logs full URLs at INFO
    install_secret_redaction()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="finesse_sync", description="Client Master Sync (Finesse)")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("setup")
    sp = sub.add_parser("serve")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--open", action="store_true", help="open the tool in the default browser")
    sub.add_parser("sync")
    sub.add_parser("test-login")
    dp = sub.add_parser("discover")
    dp.add_argument("--show", action="store_true", help="open a visible browser window")
    sub.add_parser("status")
    sub.add_parser("genkey")
    sub.add_parser("gentoken")
    a = ap.parse_args(argv)

    if a.cmd == "genkey":
        from .security import generate_key
        print(generate_key())
        return 0
    if a.cmd == "setup":
        from .wizard import run
        return run()
    if a.cmd == "gentoken":
        print(secrets.token_urlsafe(32))
        return 0

    _setup_logging(a.verbose)
    from . import db
    from .config import ConfigError, get_settings

    try:
        if a.cmd == "serve":
            import uvicorn
            from .security import Cipher
            Cipher()  # fail fast on a missing / bad key
            s = get_settings()
            if not s.admin_tokens:
                print("WARNING: CNE_ADMIN_TOKENS is empty — nobody can open the Client Master.", file=sys.stderr)
            if a.open:
                import threading
                import webbrowser
                threading.Timer(2.0, lambda: webbrowser.open(f"http://127.0.0.1:{a.port}/")).start()
            print(f"\nContract Note Extractor is running at http://127.0.0.1:{a.port}/ — keep this window open.\n")
            from .api import app
            uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
            return 0

        db.init_db()
        if a.cmd == "sync":
            from .sync import run_sync
            res = run_sync("cli")
            print(json.dumps({k: res[k] for k in ("status", "method", "fetched", "added", "updated", "flagged_missing",
                                                  "invalid_pan", "error_count", "message")}, indent=2))
            for e in res["errors"][:50]:
                print("  -", e)
            return 0 if res["status"] in ("success", "partial") else 1

        if a.cmd == "status":
            from . import master, sync
            print(json.dumps({**sync.status(), "counts": master.counts()}, indent=2, default=str))
            return 0

        if a.cmd == "test-login":
            from .finesse import FinesseClient
            with FinesseClient() as fc:
                fc.store.clear()
                fc.login()
            print("Finesse login OK — session saved (encrypted) for reuse.")
            return 0

        if a.cmd == "discover":
            return _discover(a.show)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        from .config import redact
        print(f"Error: {redact(str(e))}", file=sys.stderr)
        return 1
    return 0


def _discover(show: bool) -> int:
    """Log in with a real browser, open the client list and save every XHR/fetch
    call (URL, method, status, JSON keys; never bodies with credentials) to
    data/discovery.json, highlighting calls that look like the client list."""
    from .config import get_settings
    from .finesse import BrowserSession
    from .records import LayoutChangedError

    s = get_settings()
    s.require_finesse_credentials()
    out = s.data_dir / "discovery.json"
    with BrowserSession(s, headless=not show, capture=True) as b:
        b.login()
        print("Logged in. Opening the client list…")
        note = ""
        try:
            b.open_client_list()
            b._maximize_page_size()
        except LayoutChangedError as e:
            note = str(e)
            print("Could not open the client list automatically:", e)
            if show:
                input("Open the client list manually in the browser window, then press Enter here… ")
        links = b.page.evaluate("""() => [...document.querySelectorAll('a,button')]
            .map(e => ({text:(e.innerText||e.title||'').trim().slice(0,60), href:e.getAttribute('href')||''}))
            .filter(x => /export|excel|csv|download|xls/i.test(x.text + ' ' + x.href))""")
        report = {"page_url": b.page.url, "note": note, "export_links": links, "calls": b.captured}
    s.data_dir.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    hits = [c for c in report["calls"] if c.get("looks_like_client_list")]
    print(f"\nSaved {len(report['calls'])} network calls to {out}")
    if hits:
        print("\nLikely client-list endpoints (set FINESSE_API_URL to one of these):")
        for c in hits:
            print(f"  {c['method']} {c['url']}  ({c.get('records_found')} rows; keys: {', '.join(c.get('sample_keys', [])[:8])})")
    if links:
        print("\nExport / download controls on the page:")
        for l in links:
            print(f"  {l['text']!r} -> {l['href']}")
    if not hits and not links:
        print("No JSON client list or export link spotted — the Playwright table reader will be used.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
