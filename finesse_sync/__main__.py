"""Command line:

  python -m finesse_sync setup        first-time setup wizard (Finesse login, keys, tokens)
  python -m finesse_sync serve        start the API + scheduler (and serve the extractor UI)
  python -m finesse_sync sync         run one sync now (for cron / Task Scheduler)
  python -m finesse_sync test-login   log in to Finesse and report success / failure
  python -m finesse_sync status       last sync + counts
"""
from __future__ import annotations

import argparse
import json
import logging
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
    sp.add_argument("--port", type=int, default=None, help="default: CNE_PORT in .env, else 8766")
    sp.add_argument("--open", action="store_true", help="open the tool in the default browser")
    sub.add_parser("sync")
    sub.add_parser("test-login")
    sub.add_parser("status")
    a = ap.parse_args(argv)

    if a.cmd == "setup":
        from .wizard import run
        return run()

    _setup_logging(a.verbose)
    from . import db
    from .config import ConfigError, get_settings

    try:
        if a.cmd == "serve":
            import uvicorn
            from .security import Cipher
            Cipher()  # fail fast on a missing / bad key
            s = get_settings()
            port = a.port or s.port
            if not s.admin_tokens:
                print("WARNING: CNE_ADMIN_TOKENS is empty — nobody can open the Client Master.", file=sys.stderr)
            if a.open:
                import threading
                import webbrowser
                threading.Timer(2.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}/")).start()
            print(f"\nContract Note Extractor is running at http://127.0.0.1:{port}/ — keep this window open.\n")
            from .api import app
            uvicorn.run(app, host=a.host, port=port, log_level="warning")
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

    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001
        from .config import redact
        print(f"Error: {redact(str(e))}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
