import sqlite3 as sql
from pathlib import Path


class SQLManager:
    def __init__(self, path=None):
        # Chemin stable, même si l'IDE change le dossier de travail.
        self.path = Path(path) if path else Path(__file__).resolve().parents[1] / '.cache' / 'data.db'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sql.connect(self.path)
        self.cur = self.conn.cursor()
        self.cur.executescript('''
                               CREATE TABLE IF NOT EXISTS visited
                               (
                                   id  INTEGER PRIMARY KEY AUTOINCREMENT,
                                   url TEXT UNIQUE NOT NULL
                               );
                               CREATE TABLE IF NOT EXISTS queue
                               (
                                   id    INTEGER PRIMARY KEY AUTOINCREMENT,
                                   url   TEXT UNIQUE NOT NULL,
                                   type_ TEXT        NOT NULL
                               );
                               CREATE TABLE IF NOT EXISTS audios
                               (
                                   id        INTEGER PRIMARY KEY AUTOINCREMENT,
                                   url       TEXT,
                                   audio_url TEXT,
                                   file_name TEXT,
                                   title     TEXT,
                                   desc_     TEXT,
                                   lang      TEXT,
                                   cite      TEXT,
                                   license   TEXT
                               );
                               CREATE INDEX IF NOT EXISTS audios_page_source ON audios (url, audio_url);
                               ''')
        columns = {row[1] for row in self.cur.execute('PRAGMA table_info(audios)')}
        for name in ('lieu', 'doi'):
            if name not in columns:
                self.cur.execute(f'ALTER TABLE audios ADD COLUMN {name} TEXT')
        self.conn.commit()

    def add_visited(self, url):
        self.cur.execute('INSERT OR IGNORE INTO visited (url) VALUES (?)', (url,))

    def is_visited(self, url):
        return self.cur.execute('SELECT 1 FROM visited WHERE url = ?', (url,)).fetchone() is not None

    def create_audio(self, url, audio_url, title, desc_, lang, cite, license_, lieu='', doi=''):
        # Reprise sans réinsérer un audio déjà sauvegardé pour la même page.
        self.cur.execute('''
                         INSERT INTO audios (url, audio_url, file_name, title, desc_, lang, cite, license, lieu, doi)
                         SELECT ?,
                                ?,
                                '',
                                ?,
                                ?,
                                ?,
                                ?,
                                ?,
                                ?,
                                ?
                         WHERE NOT EXISTS (SELECT 1 FROM audios WHERE url = ? AND audio_url = ?)
                         ''', (url, audio_url, title, desc_, lang, cite, license_, lieu, doi, url, audio_url))

    def save_to_queue(self, res: dict):
        added = []
        for url, type_ in res.items():
            if not self.is_visited(url):
                self.cur.execute('INSERT OR IGNORE INTO queue (url, type_) VALUES (?, ?)', (url, type_))
                if self.cur.rowcount:
                    added.append((url, type_))
        return added

    def get_queue(self):
        return self.cur.execute('SELECT url, type_ FROM queue ORDER BY id').fetchall()

    def get_queue_size(self):
        return self.cur.execute('SELECT COUNT(*) FROM queue').fetchone()[0]

    def remove_from_queue(self, url):
        self.cur.execute('DELETE FROM queue WHERE url = ?', (url,))

    def commit(self):
        self.conn.commit()

    def rollback(self):
        self.conn.rollback()

    def close(self):
        self.cur.close()
        self.conn.close()
