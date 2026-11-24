"""SQLite job and settings store with secure row erasure."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = '''
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    gid TEXT DEFAULT '',
    kind TEXT,
    source TEXT,
    name TEXT,
    owner TEXT,
    created REAL,
    finished REAL,
    status TEXT,
    error TEXT DEFAULT '',
    total INTEGER DEFAULT 0,
    completed INTEGER DEFAULT 0,
    files TEXT DEFAULT '[]',
    shared_as TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
'''


class Store:
    """Thread-safe SQLite store with PRAGMA secure_delete = ON."""

    def __init__(self, path: Path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        # Overwrite deleted rows immediately rather than leaving links readable in free pages.
        self.db.execute('PRAGMA secure_delete = ON')
        self.db.executescript(SCHEMA)
        self.lock = threading.RLock()

    def all(self) -> list[dict]:
        with self.lock:
            return [dict(row) for row in self.db.execute('SELECT * FROM jobs ORDER BY created DESC')]

    def get(self, job_id: str) -> dict | None:
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
        return dict(row) if row else None

    def add(self, job: dict) -> None:
        with self.lock:
            columns = ','.join(job.keys())
            placeholders = ','.join('?' * len(job))
            self.db.execute(f'INSERT INTO jobs ({columns}) VALUES ({placeholders})', tuple(job.values()))

    def update(self, job_id: str, **fields) -> None:
        if not fields:
            return
        with self.lock:
            assignments = ', '.join(f'{key} = ?' for key in fields)
            self.db.execute(f'UPDATE jobs SET {assignments} WHERE id = ?', (*fields.values(), job_id))

    def delete(self, job_id: str) -> None:
        with self.lock:
            self.db.execute('DELETE FROM jobs WHERE id = ?', (job_id,))

    def setting(self, key: str, default: str | None = None) -> str | None:
        with self.lock:
            row = self.db.execute('SELECT value FROM settings WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.lock:
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?, ?)', (key, value))

    def close(self) -> None:
        with self.lock:
            self.db.close()
