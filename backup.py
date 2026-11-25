from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

from app import DATABASE_PATH

BACKUP_DIR = Path(__file__).resolve().parent / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
output = BACKUP_DIR / f"quiz_{datetime.now():%Y%m%d_%H%M%S}.db"

source = sqlite3.connect(DATABASE_PATH)
target = sqlite3.connect(output)
try:
    source.backup(target)
finally:
    target.close()
    source.close()
print(output)
