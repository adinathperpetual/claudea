"""Build the source zips into dist/ (the .exe zip comes from packaging/exe/build_exe.py).

    python packaging/make_zips.py

  ContractNoteExtractor.zip                 Python version (SETUP.bat / START.bat)
  ContractNoteExtractor-exe-build-kit.zip   what BUILD_EXE.bat needs to build the .exe
  ContractNoteExtractor-all-files.zip       everything, including the tests
Files are taken from the last commit (git archive), so .env and data/ never get in.
"""
from __future__ import annotations

import io
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOP = "ContractNoteExtractor"
NOT_FOR_USERS = ("tests/", "requirements-dev.txt", ".github/", ".gitignore", ".gitattributes")
BUILD_KIT = ("BUILD_EXE.bat", "requirements.txt", "index.html", ".env.example", "finesse_sync/",
             "packaging/exe/", "START_HERE.txt", "README.md")


def _files() -> list[tuple[str, bytes]]:
    tar = subprocess.run(["git", "archive", "--format=tar", "HEAD"], cwd=ROOT, check=True, capture_output=True).stdout
    with tarfile.open(fileobj=io.BytesIO(tar)) as t:
        return [(m.name, t.extractfile(m).read()) for m in t.getmembers() if m.isfile()]


def _zip(name: str, files: list[tuple[str, bytes]], keep) -> Path:
    out = ROOT / "dist" / name
    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in files:
            if keep(path):
                z.writestr(f"{TOP}/{path}", data)
    print(out)
    return out


def main() -> int:
    files = _files()
    _zip("ContractNoteExtractor.zip", files, lambda p: not p.startswith(NOT_FOR_USERS))
    _zip("ContractNoteExtractor-exe-build-kit.zip", files, lambda p: p.startswith(BUILD_KIT))
    _zip("ContractNoteExtractor-all-files.zip", files, lambda p: True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
