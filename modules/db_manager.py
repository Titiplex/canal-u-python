import sqlite3 as sql
from typing import Any
from pathlib import Path


class SQLManager:
    def __init__(self):
        Path(".cache").mkdir(parents=True, exist_ok=True)
        self.conn = sql.connect(".cache/data.db")
        self.cur = self.conn.cursor()

        self.cur.execute("""
                         CREATE TABLE IF NOT EXISTS visited(
                             id
                             INTEGER
                             NOT
                             NULL
                             PRIMARY
                             KEY
                             AUTOINCREMENT,
                             url
                             TEXT
                             UNIQUE
                             NOT
                             NULL
                         )
                         """)

        self.cur.execute("""
                         CREATE TABLE IF NOT EXISTS queue(
                             id
                             INTEGER
                             NOT
                             NULL
                             PRIMARY
                             KEY
                             AUTOINCREMENT,
                             url
                             TEXT
                             UNIQUE
                             NOT
                             NULL,
                             type_
                             TEXT
                             NOT
                             NULL
                         )
                         """)

        self.cur.execute("""
                         CREATE TABLE IF NOT EXISTS audios(
                             id
                             INTEGER
                             NOT
                             NULL
                             PRIMARY
                             KEY
                             AUTOINCREMENT,
                             url
                             TEXT,
                             audio_url
                             TEXT,
                             file_name
                             TEXT,
                             title TEXT,
                             desc_
                             TEXT,
                             lang
                             TEXT,
                             cite
                             TEXT,
                             license
                             TEXT
                         )
                         """)

    def add_visited(self, url: str) -> None:
        self.cur.execute("INSERT INTO visited VALUES (?, ?)", (url,))

    def is_visited(self, url: str) -> bool:
        self.cur.execute("SELECT * FROM visited WHERE url = ?", (url,))
        return self.cur.fetchone() is not None

    def create_audio(self, url: str, audio_url: str,  title: str, desc_: str, lang: str, cite: str, license_: str) -> None:
        self.cur.execute("""
                         INSERT INTO audios
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                         """, (url, audio_url, "", title, desc_, lang, cite, license_,))

    def commit(self) -> None:
        self.conn.commit()

    def close(self) -> None:
        self.cur.close()
        self.conn.close()

    def save_to_queue(self, res: dict):
        for a, b in res:
            if not self.is_visited(a):
                self.cur.execute("""
                                 INSERT INTO queue
                                 VALUES (?, ?)
                                 """, (a, b))
                self.add_visited(a)

    def get_queue(self) -> list[Any]:
        self.cur.execute("SELECT * FROM queue")
        return self.cur.fetchall()

    def get_queue_size(self) -> int:
        self.cur.execute("SELECT COUNT(*) FROM queue")
        return self.cur.fetchone()[0]

    def remove_from_queue(self, url: str) -> None:
        self.cur.execute("DELETE FROM queue WHERE url = ?", (url,))