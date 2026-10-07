"""First-time setup wizard: `python -m finesse_sync setup`.

Asks for the Finesse login (user id, password, PAN), generates the encryption
key and access tokens, writes .env (keeping anything already there), saves the
tokens to data/ACCESS_TOKENS.txt and optionally tests the Finesse login.
"""
from __future__ import annotations

import getpass
import re
import secrets
from pathlib import Path

from .config import ROOT
from .security import PAN_RE, generate_key

ENV = ROOT / ".env"
EXAMPLE = ROOT / ".env.example"


def _read_env(path: Path) -> dict[str, str]:
    vals: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Z_][A-Z0-9_]*)\s*=(.*)$", line)
            if m:
                vals[m.group(1)] = m.group(2).strip()
    return vals


def _write_env(values: dict[str, str]) -> None:
    """Fill values into the .env.example layout so comments stay with each setting."""
    template = EXAMPLE.read_text(encoding="utf-8") if EXAMPLE.exists() else ""
    out, done = [], set()
    for line in template.splitlines():
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in values:
            out.append(f"{m.group(1)}={values[m.group(1)]}")
            done.add(m.group(1))
        else:
            out.append(line)
    extra = [f"{k}={v}" for k, v in values.items() if k not in done]
    if extra:
        out += ["", "# ---- other settings ----", *extra]
    ENV.write_text("\n".join(out) + "\n", encoding="utf-8")
    try:
        ENV.chmod(0o600)
    except OSError:
        pass


def _ask(prompt: str, current: str = "", secret: bool = False, check=None, hint: str = "") -> str:
    shown = " [already set — press Enter to keep]" if current else ""
    while True:
        v = (getpass.getpass if secret else input)(f"{prompt}{shown}: ").strip()
        if not v and current:
            return current
        if not v:
            print("  This is required.")
            continue
        if check and not check(v):
            print(f"  {hint}")
            continue
        return v


def run() -> int:
    print("\n=== Contract Note Extractor — Client Master Sync setup ===\n")
    vals = _read_env(ENV)

    print("Step 1 of 3 — Finesse login (stored only in the local .env file on this PC)")
    vals["FINESSE_USER_ID"] = _ask("  Finesse User ID", vals.get("FINESSE_USER_ID", ""))
    vals["FINESSE_PASSWORD"] = _ask("  Finesse Password (typing is hidden)", vals.get("FINESSE_PASSWORD", ""), secret=True)
    vals["FINESSE_PAN"] = _ask("  PAN used for Finesse login", vals.get("FINESSE_PAN", ""),
                               check=lambda v: bool(PAN_RE.match(v.upper())),
                               hint="PAN must be 5 letters + 4 digits + 1 letter, e.g. ABCDE1234F").upper()

    print("\nStep 2 of 3 — security keys")
    if not vals.get("CNE_ENCRYPTION_KEY"):
        vals["CNE_ENCRYPTION_KEY"] = generate_key()
        print("  Encryption key created.")
    else:
        print("  Encryption key already present — kept (changing it would make saved data unreadable).")
    if not vals.get("CNE_ADMIN_TOKENS"):
        vals["CNE_ADMIN_TOKENS"] = secrets.token_urlsafe(24)
    if not vals.get("CNE_EXTRACTOR_TOKENS"):
        vals["CNE_EXTRACTOR_TOKENS"] = secrets.token_urlsafe(24)
    _write_env(vals)

    data = Path(vals.get("CNE_DATA_DIR") or ROOT / "data")
    data.mkdir(parents=True, exist_ok=True)
    tok = data / "ACCESS_TOKENS.txt"
    tok.write_text(
        "Contract Note Extractor — access tokens (keep private)\n"
        "======================================================\n\n"
        f"ADMIN TOKEN (Client Master screen, Sync from Finesse):\n  {vals['CNE_ADMIN_TOKENS'].split(',')[0]}\n\n"
        f"EXTRACTOR TOKEN (only needed on other PCs; this PC connects automatically):\n  {vals['CNE_EXTRACTOR_TOKENS'].split(',')[0]}\n\n"
        "ENCRYPTION KEY BACKUP — without it saved PANs/passwords cannot be read:\n"
        f"  {vals['CNE_ENCRYPTION_KEY']}\n", encoding="utf-8")
    print(f"  Settings saved to {ENV.name}. Tokens and key backup saved to {tok}")

    print("\nStep 3 of 3 — test the Finesse login")
    if input("  Test the Finesse login now? (Y/n): ").strip().lower() in ("", "y", "yes"):
        import os

        from dotenv import load_dotenv

        from . import config
        # the values were just written to .env; load them (they weren't there at start-up)
        for k, v in vals.items():
            os.environ[k] = v
        load_dotenv(ENV, override=True)
        config.reset_settings()
        from .finesse import FinesseClient
        try:
            with FinesseClient() as fc:
                fc.store.clear()
                fc.login()
            print("  [OK] Finesse login OK.")
        except Exception as e:  # noqa: BLE001
            print(f"  [FAILED] Login test failed: {config.redact(str(e))}")
            print("    You can fix the details by running SETUP again, or send this message to IT.")
    print("\nSetup complete. Double-click START to open the tool.\n")
    return 0
