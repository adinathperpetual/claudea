"""Build the Windows installer dist/ContractNoteExtractor-Setup.exe.

    python packaging/exe/build_exe.py

PyInstaller first makes the program in dist/exe/ContractNoteExtractor/ (the .exe and its
_internal folder); Inno Setup 6 then packs that folder into the
one-file installer. Build on Windows (PyInstaller does not cross-compile). No Python is
needed on the PCs that install the result.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
NAME = "ContractNoteExtractor"
SEP = ";" if sys.platform == "win32" else ":"


def main() -> int:
    import PyInstaller.__main__

    out = ROOT / "dist" / "exe"
    work = ROOT / "build" / "exe"
    PyInstaller.__main__.run([
        str(HERE / "cne_exe.py"),
        "--name", NAME,
        "--onedir", "--console", "--noconfirm", "--clean",
        "--distpath", str(out), "--workpath", str(work), "--specpath", str(work),
        "--paths", str(ROOT),
        "--add-data", f"{ROOT / 'index.html'}{SEP}.",
        "--add-data", f"{ROOT / '.env.example'}{SEP}.",
        "--collect-submodules", "finesse_sync",
        "--collect-all", "playwright",
        "--collect-submodules", "pypdf",
        "--hidden-import", "apscheduler.triggers.cron",
        "--hidden-import", "apscheduler.schedulers.background",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
    ])
    print(f"\nBuilt {out / NAME}")
    return build_installer()


def _iscc() -> str | None:
    found = shutil.which("ISCC") or shutil.which("iscc")
    if found:
        return found
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"),
                 os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs")):
        exe = Path(base or "") / "Inno Setup 6" / "ISCC.exe"
        if base and exe.exists():
            return str(exe)
    return None


def build_installer() -> int:
    if sys.platform != "win32":
        print("The installer can only be made on Windows.")
        return 0
    iscc = _iscc()
    if not iscc:
        print("\nInno Setup 6 is not installed, so the installer could not be made.\n"
              "Install it from https://jrsoftware.org/isdl.php and run this again.")
        return 1
    version = os.environ.get("CNE_VERSION") or date.today().strftime("%Y.%m.%d")
    subprocess.run([iscc, f"/DAppVersion={version}", str(ROOT / "packaging" / "installer" / "ContractNoteExtractor.iss")],
                   check=True)
    print(f"Installer {ROOT / 'dist' / 'ContractNoteExtractor-Setup.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
