# ruff: noqa: E501
"""Back up every VENTHORA store, and prove a backup can be restored.

    python scripts/backup_stores.py backup  <data-dir> <backup-root>     # a consistent copy of every *.db, while the service runs
    python scripts/backup_stores.py verify  <backup-dir>                 # every copy opens and passes SQLite's integrity check
    python scripts/backup_stores.py restore <backup-dir> <data-dir>      # put a backup back (the service must be stopped)

`backup` uses SQLite's online backup, so a store that is being written is copied whole, never half-way through a
transaction. Each run goes to its own folder, <backup-root>/<UTC timestamp>, with a manifest (file, bytes, SHA-256,
row count per table). `restore` refuses a backup that does not pass `verify`, and keeps what it replaces next to it
as <name>.before-restore. Nothing is deleted by any command.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def _tables(db: Path) -> dict[str, int]:
    with closing(sqlite3.connect(db)) as c:
        names = [r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")]
        return {n: c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0] for n in sorted(names)}


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def backup(data_dir: Path, backup_root: Path, now: datetime | None = None) -> Path:
    stores = sorted(p for p in data_dir.rglob("*.db") if p.is_file())
    if not stores:
        raise SystemExit(f"no store (*.db) under {data_dir}")
    out = backup_root / (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=False)
    manifest = {"taken_at": (now or datetime.now(UTC)).isoformat(timespec="seconds"), "source": str(data_dir), "stores": []}
    for src in stores:
        rel = src.relative_to(data_dir)
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(src, timeout=30)) as s, closing(sqlite3.connect(dst)) as d:
            s.backup(d)
        manifest["stores"].append({"file": rel.as_posix(), "bytes": dst.stat().st_size, "sha256": _sha(dst), "rows": _tables(dst)})
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return out


def verify(backup_dir: Path) -> list[str]:
    """The problems found; an empty list means the backup can be restored."""
    manifest_file = backup_dir / "manifest.json"
    if not manifest_file.is_file():
        return [f"{backup_dir} has no manifest.json: it is not a backup made by this script"]
    problems = []
    for rec in json.loads(manifest_file.read_text(encoding="utf-8"))["stores"]:
        path = backup_dir / rec["file"]
        if not path.is_file():
            problems.append(f"{rec['file']}: missing")
            continue
        if _sha(path) != rec["sha256"]:
            problems.append(f"{rec['file']}: changed since the backup was taken")
            continue
        with closing(sqlite3.connect(path)) as c:
            check = c.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            problems.append(f"{rec['file']}: SQLite integrity check says {check}")
        elif _tables(path) != rec["rows"]:
            problems.append(f"{rec['file']}: row counts differ from the manifest")
    return problems


def restore(backup_dir: Path, data_dir: Path) -> list[str]:
    problems = verify(backup_dir)
    if problems:
        raise SystemExit("not restored, the backup is not sound:\n  " + "\n  ".join(problems))
    done = []
    for rec in json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))["stores"]:
        dst = data_dir / rec["file"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            shutil.copy2(dst, dst.with_name(dst.name + ".before-restore"))
        shutil.copy2(backup_dir / rec["file"], dst)
        done.append(rec["file"])
    return done


def main(argv: list[str]) -> int:
    if len(argv) == 3 and argv[0] == "backup":
        out = backup(Path(argv[1]), Path(argv[2]))
        print(f"backup written to {out}")
        return 0
    if len(argv) == 2 and argv[0] == "verify":
        problems = verify(Path(argv[1]))
        print("sound: every store opens, matches its checksum and its row counts" if not problems else "\n".join(problems))
        return 1 if problems else 0
    if len(argv) == 3 and argv[0] == "restore":
        print("restored: " + ", ".join(restore(Path(argv[1]), Path(argv[2]))))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
