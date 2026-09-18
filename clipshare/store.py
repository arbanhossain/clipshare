"""SQLite-backed clipboard history store with content-hash dedup."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hash TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('text', 'image')),
    text TEXT,
    image BLOB,
    source TEXT NOT NULL,
    created_at REAL NOT NULL,
    pinned INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_created ON items (created_at DESC);
"""


def digest(payload: str | bytes) -> str:
    if isinstance(payload, str):
        payload = payload.encode("utf-8", "surrogatepass")
    return hashlib.sha256(payload).hexdigest()


class Store:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def add(
        self, kind: str, payload: str | bytes, source: str, ts: float | None = None
    ) -> tuple[bool, int | None]:
        with self._lock:
            return self._add_locked(kind, payload, source, ts)

    def _add_locked(self, kind, payload, source, ts):
        h = digest(payload)
        row = self._conn.execute("SELECT id FROM items WHERE hash = ?", (h,)).fetchone()
        if row:
            return False, row["id"]
        created = ts if ts is not None else time.time()
        cur = self._conn.execute(
            "INSERT INTO items (hash, kind, text, image, source, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (
                h,
                kind,
                payload if kind == "text" else None,
                payload if kind == "image" else None,
                source,
                created,
            ),
        )
        self._conn.commit()
        return True, cur.lastrowid

    def get(self, item_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
        return dict(row) if row else None

    def by_hash(self, h: str) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE hash = ?", (h,)).fetchone()
        return dict(row) if row else None

    def recent(self, limit: int = 200) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, hash, kind, text, source, created_at, pinned FROM items "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def search(self, query: str, limit: int = 200) -> list[dict]:
        like = f"%{query}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT id, hash, kind, text, source, created_at, pinned FROM items "
                "WHERE kind = 'text' AND text LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete(self, item_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM items WHERE id = ?", (item_id,))
            self._conn.commit()
        return cur.rowcount > 0

    def set_pinned(self, item_id: int, pinned: bool) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE items SET pinned = ? WHERE id = ?",
                (1 if pinned else 0, item_id),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def prune(self, retention_days: int, history_limit: int) -> int:
        with self._lock:
            return self._prune_locked(retention_days, history_limit)

    def _prune_locked(self, retention_days, history_limit) -> int:
        cutoff = time.time() - retention_days * 86400
        cur = self._conn.execute(
            "DELETE FROM items WHERE pinned = 0 AND created_at < ?", (cutoff,)
        )
        removed = cur.rowcount
        excess = (
            self._conn.execute("SELECT COUNT(*) FROM items WHERE pinned = 0").fetchone()[0]
            - history_limit
        )
        if excess > 0:
            rows = self._conn.execute(
                "SELECT id FROM items WHERE pinned = 0 ORDER BY created_at ASC LIMIT ?",
                (excess,),
            ).fetchall()
            self._conn.executemany(
                "DELETE FROM items WHERE id = ?", [(r["id"],) for r in rows]
            )
            removed += len(rows)
        self._conn.commit()
        return removed
