"""Download the official ECB euro reference-rate history for Horizon's FX stress.

Run: python scripts/import_ecb.py
Writes data/imports/eurofxref-hist.csv and prints its SHA-256.
"""
import hashlib
import io
import urllib.request
import zipfile
from pathlib import Path

URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
OUT = Path(__file__).resolve().parents[1] / "data" / "imports" / "eurofxref-hist.csv"


def main() -> None:
    with urllib.request.urlopen(URL, timeout=60) as res:
        archive = zipfile.ZipFile(io.BytesIO(res.read()))
    raw = archive.read("eurofxref-hist.csv")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(raw)
    print(f"saved {OUT} ({len(raw):,} bytes) sha256={hashlib.sha256(raw).hexdigest()}")


if __name__ == "__main__":
    main()
