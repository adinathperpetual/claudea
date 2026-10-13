"""Entry point of ContractNoteExtractor.exe (built with PyInstaller, see build_exe.py).

Double-clicked (no arguments): runs the setup wizard the first time, then starts the
tool and opens it in the browser. With arguments it is the normal command line, e.g.
``ContractNoteExtractor.exe sync``. ``--pause`` (used by the Start-menu shortcuts) keeps
the window open at the end so the result can be read.
"""
from __future__ import annotations

import sys


def run(args: list[str]) -> int:
    from finesse_sync.__main__ import main
    from finesse_sync.config import ROOT

    if not args:
        env = ROOT / ".env"
        if not env.exists():
            rc = main(["setup"])
            if rc or not env.exists():
                return rc or 1
            from dotenv import load_dotenv

            from finesse_sync import config
            load_dotenv(env, override=True)        # written just now, after start-up
            config.reset_settings()
        args = ["serve", "--open"]
    return main(args)


if __name__ == "__main__":
    args = sys.argv[1:]
    pause = "--pause" in args
    args = [a for a in args if a != "--pause"]
    try:
        code = run(args)
    except KeyboardInterrupt:
        code = 0
    except SystemExit as e:                        # argparse errors / --help
        code = e.code if isinstance(e.code, int) else 0
    if (pause or code) and sys.stdin and sys.stdin.isatty():
        input("\nPress Enter to close this window… ")
    sys.exit(code)
