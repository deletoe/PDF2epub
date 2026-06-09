#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parent
PLUGIN_DIR = ROOT / "calibre_plugin"
DIST_DIR = ROOT / "dist"
OUTPUT = DIST_DIR / "LocalPdfOcr.zip"


def main() -> int:
    DIST_DIR.mkdir(exist_ok=True)
    with ZipFile(OUTPUT, "w", ZIP_DEFLATED) as zf:
        for path in sorted(PLUGIN_DIR.rglob("*")):
            if path.is_dir() or "__pycache__" in path.parts:
                continue
            zf.write(path, path.relative_to(PLUGIN_DIR).as_posix())
    print(OUTPUT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
