# database.py
import sqlite3
import threading
from pathlib import Path

from server.config import CONFIG

# Resolve relative to the project root, not the current working directory (H13)
DB_PATH = Path(__file__).resolve().parent.parent / CONFIG["database_file"]

# One shared connection for the whole process; the lock serialises access
_conn = None
_lock = threading.RLock()


def _get_connection():
    global _conn
    with _lock:
        if _conn is None:
            _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=30)
            _conn.execute("PRAGMA journal_mode=WAL")
            _conn.execute("PRAGMA busy_timeout=30000")
        return _conn


class Database:

    def __init__(self):
        self.conn = _get_connection()
        self.create_tables()

    def _execute(self, sql: str, params=()):
        """Run one statement under the shared lock. Returns (rows, rowcount)."""
        with _lock:
            cursor = self.conn.execute(sql, params)
            rows = cursor.fetchall()
            self.conn.commit()
            return rows, cursor.rowcount

    def create_tables(self):
        self._execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                status TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                filepath TEXT
            )
            """
        )
        # migrate databases created before the filepath column existed
        columns = [row[1] for row in self._execute("PRAGMA table_info(videos)")[0]]
        if "filepath" not in columns:
            self._execute("ALTER TABLE videos ADD COLUMN filepath TEXT")

    def get_status(self, url: str):
        rows, _ = self._execute("SELECT status FROM videos WHERE url = ?", (url,))
        return rows[0][0] if rows else None

    def add_video(self, url: str, status: str):
        _, rowcount = self._execute(
            "INSERT OR IGNORE INTO videos (url, status) VALUES (?, ?)",
            (url, status),
        )
        return rowcount > 0

    def update_status(self, url: str, status: str):
        self._execute("UPDATE videos SET status = ? WHERE url = ?", (status, url))

    def set_filepath(self, url: str, filepath: str):
        self._execute("UPDATE videos SET filepath = ? WHERE url = ?", (filepath, url))

    def get_queued(self):
        rows, _ = self._execute("SELECT url FROM videos WHERE status = 'queued' ORDER BY id")
        return [row[0] for row in rows]

    def is_done(self, url: str):
        """True if the video finished and its output file (if one was recorded) still exists."""
        rows, _ = self._execute("SELECT status, filepath FROM videos WHERE url = ?", (url,))
        if not rows:
            return False
        status, filepath = rows[0]
        if status not in ("completed", "downloaded"):
            return False
        # rows from older versions have no filepath: trust the status
        return not filepath or Path(filepath).is_file()

    def claim(self, url: str):
        """Atomically mark a video as 'downloading'. False if someone else already did (H12)."""
        _, rowcount = self._execute(
            "UPDATE videos SET status = 'downloading' "
            "WHERE url = ? AND COALESCE(status, '') != 'downloading'",
            (url,),
        )
        return rowcount > 0

    def reset_interrupted(self):
        """Videos left on 'downloading' by a crash/restart go back to 'queued' (H2)."""
        _, rowcount = self._execute(
            "UPDATE videos SET status = 'queued' WHERE status = 'downloading'"
        )
        return rowcount