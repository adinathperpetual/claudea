"""Build ContractNoteExtractor.exe (Windows) with PyInstaller.

    python packaging/exe/build_exe.py

Output: dist/exe/ContractNoteExtractor/ (the .exe + its _internal folder + helper .bat
files) and dist/ContractNoteExtractor-exe.zip. Build on Windows to get a Windows .exe —
PyInstaller does not cross-compile. No Python is needed on the PCs that run the result.
"""
from __future__ import annotations

import shutil
import sys
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
