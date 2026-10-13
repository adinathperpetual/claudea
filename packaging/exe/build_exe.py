"""Build ContractNoteExtractor.exe (Windows) with PyInstaller.

    python packaging/exe/build_exe.py

Output: dist/exe/ContractNoteExtractor/ (the .exe + its _internal folder + helper .bat
files), dist/ContractNoteExtractor-exe.zip and, when Inno Setup 6 is installed, the
one-file Windows installer dist/ContractNoteExtractor-Setup.exe. Build on Windows to get a Windows .exe —
PyInstaller does not cross-compile. No Python is needed on the PCs that run the result.
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
        "--hidden-import", "apscheduler.triggers.cron",
        "--hidden-import", "apscheduler.schedulers.background",
        "--exclude-module", "tkinter",
        "--exclude-module", "matplotlib",
        "--exclude-module", "pytest",
    ])
    app = out / NAME
    for f in (HERE / "extras").iterdir():
        shutil.copy2(f, app / f.name)
    zip_base = ROOT / "dist" / f"{NAME}-exe"
    shutil.make_archive(str(zip_base), "zip", root_dir=out, base_dir=NAME)
    print(f"\nBuilt {app}\nZip   {zip_base}.zip")
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
        return 0
    iscc = _iscc()
    if not iscc:
        print("\nInno Setup 6 is not installed, so no installer was made (the .exe zip is ready).\n"
              "Install it from https://jrsoftware.org/isdl.php and run this again to get\n"
              "dist\\ContractNoteExtractor-Setup.exe.")
        return 0
    version = os.environ.get("CNE_VERSION") or date.today().strftime("%Y.%m.%d")
    subprocess.run([iscc, f"/DAppVersion={version}", str(ROOT / "packaging" / "installer" / "ContractNoteExtractor.iss")],
                   check=True)
    print(f"Installer {ROOT / 'dist' / 'ContractNoteExtractor-Setup.exe'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
