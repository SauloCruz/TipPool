"""`python -m app.backup` (or `make backup`): safe online copy of the SQLite
DB into DATA_DIR/backups/ with a timestamp, using the sqlite3 backup API so
it's consistent even while the app is running."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from .config import Settings


def main() -> None:
    settings = Settings()
    if not settings.db_path.exists():
        raise SystemExit(f"no database at {settings.db_path} — nothing to back up")
    backups = settings.data_dir / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = backups / f"tippool-{stamp}.sqlite3"
    src = sqlite3.connect(settings.db_path)
    dst = sqlite3.connect(dest)
    with dst:
        src.backup(dst)
    src.close()
    dst.close()
    print(f"backed up {settings.db_path} -> {dest}")


if __name__ == "__main__":
    main()
