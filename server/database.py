import sqlite3
from pathlib import Path

from server.config import CONFIG

DB_PATH = Path(CONFIG["database_file"])


class Database:

    def __init__(self):
        self.conn = sqlite3.connect(
            DB_PATH,
            check_same_thread=False
        )

        self.create_tables()

    def create_tables(self):

        cursor = self.conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE,
                status TEXT,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        self.conn.commit()

    def get_status(self, url: str):

        cursor = self.conn.cursor()

        cursor.execute(
            "SELECT status FROM videos WHERE url = ?",
            (url,),
        )

        row = cursor.fetchone()

        return row[0] if row else None

    def add_video(self, url: str, status: str):

        cursor = self.conn.cursor()

        cursor.execute(
            """
            INSERT OR IGNORE INTO videos (url, status)
            VALUES (?, ?)
            """,
            (url, status),
        )

        self.conn.commit()

        return cursor.rowcount > 0

    def update_status(self, url: str, status: str):

        cursor = self.conn.cursor()

        cursor.execute(
            """
            UPDATE videos
            SET status = ?
            WHERE url = ?
            """,
            (status, url),
        )

        self.conn.commit()
    
    def get_queued(self):
        cursor = self.conn.cursor()

        cursor.execute(
            "SELECT url FROM videos WHERE status = 'queued'"
        )

        rows = cursor.fetchall()

        return [row[0] for row in rows]