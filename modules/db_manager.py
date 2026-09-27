import sqlite3 as sql
import time
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
                               CREATE TABLE IF NOT EXISTS retry_schedule
                               (
                                   kind         TEXT    NOT NULL,
                                   url          TEXT    NOT NULL,
                                   attempts     INTEGER NOT NULL,
                                   next_attempt REAL,
                                   error        TEXT,
                                   PRIMARY KEY (kind, url)
                               );
                               CREATE TABLE IF NOT EXISTS crawl_state
                               (
                                   key   TEXT PRIMARY KEY,
                                   value REAL NOT NULL
                               );
                               CREATE TABLE IF NOT EXISTS audio_sizes
                               (
                                   audio_url    TEXT PRIMARY KEY,
                                   size_bytes   INTEGER,
                                   final_url    TEXT,
                                   status       TEXT    NOT NULL,
                                   method       TEXT,
                                   detail       TEXT,
                                   checked_at   REAL    NOT NULL,
                                   attempts     INTEGER NOT NULL DEFAULT 0,
                                   next_attempt REAL
                               );
                               CREATE INDEX IF NOT EXISTS audios_source ON audios (audio_url);
                               ''')
        columns = {row[1] for row in self.cur.execute('PRAGMA table_info(audios)')}
        for name in ('lieu', 'doi'):
            if name not in columns:
                self.cur.execute(f'ALTER TABLE audios ADD COLUMN {name} TEXT')
        self.conn.commit()

    def upgrade_parser(self):
        """Réactiver une seule fois les erreurs de l'ancien sélecteur."""
        self.cur.execute('''CREATE TABLE IF NOT EXISTS page_outcomes
                            (
                                url        TEXT PRIMARY KEY,
                                status     TEXT NOT NULL,
                                detail     TEXT NOT NULL,
                                checked_at REAL NOT NULL
                            )''')
        if self.cur.execute("SELECT 1 FROM crawl_state WHERE key='parser_v2'").fetchone():
            return 0
        self.cur.execute("""DELETE
                            FROM retry_schedule
                            WHERE kind = 'page'
                              AND error LIKE 'Page Canal-U non reconnue ou incomplète%'
                              AND url IN (SELECT url FROM queue)""")
        count = self.cur.rowcount
        self.cur.execute("INSERT INTO crawl_state(key,value) VALUES ('parser_v2',1)")
        self.commit()
        return count

    def record_outcome(self, url, status, detail=''):
        self.cur.execute('''INSERT INTO page_outcomes
                            VALUES (?, ?, ?, ?)
                            ON CONFLICT(url) DO UPDATE SET status=excluded.status,
                                                           detail=excluded.detail,
                                                           checked_at=excluded.checked_at''',
                         (url, status, detail, time.time()))

    def outcome_counts(self):
        return dict(self.cur.execute('SELECT status, COUNT(*) FROM page_outcomes GROUP BY status'))

    def add_visited(self, url):
        self.cur.execute('INSERT OR IGNORE INTO visited (url) VALUES (?)', (url,))

    def pending_audio_sizes(self):
        return [row[0] for row in self.cur.execute('''
                                                   SELECT DISTINCT a.audio_url
                                                   FROM audios a
                                                            LEFT JOIN audio_sizes s ON s.audio_url = a.audio_url
                                                   WHERE a.audio_url IS NOT NULL
                                                     AND TRIM(a.audio_url) != ''
                                                     AND (s.audio_url IS NULL OR (s.status = 'retry' AND s.next_attempt <= ?))
                                                   ORDER BY a.audio_url''', (time.time(),))]

    def audio_size_ready(self, url):
        row = self.cur.execute('SELECT status, next_attempt FROM audio_sizes WHERE audio_url=?', (url,)).fetchone()
        return row is None or (row[0] == 'retry' and row[1] is not None and row[1] <= time.time())

    def save_audio_size(self, url, result):
        self.cur.execute('''INSERT INTO audio_sizes
                                (audio_url, size_bytes, final_url, status, method, detail, checked_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT(audio_url) DO UPDATE SET size_bytes=excluded.size_bytes,
                                                                 final_url=excluded.final_url,
                                                                 status=excluded.status,
                                                                 method=excluded.method,
                                                                 detail=excluded.detail,
                                                                 checked_at=excluded.checked_at,
                                                                 next_attempt=NULL''',
                         (url, result['size_bytes'], result['final_url'], result['status'],
                          result['method'], result['detail'], time.time()))

    def defer_audio_size(self, url, error):
        row = self.cur.execute('SELECT attempts FROM audio_sizes WHERE audio_url=?', (url,)).fetchone()
        attempts = (row[0] if row else 0) + 1
        delay = max(getattr(error, 'delay', 60), min(3600, 60 * 2 ** min(attempts - 1, 10)))
        deadline = time.time() + delay
        self.cur.execute('''INSERT INTO audio_sizes
                                (audio_url, status, detail, checked_at, attempts, next_attempt)
                            VALUES (?, ?, ?, ?, ?, ?)
                            ON CONFLICT(audio_url) DO UPDATE SET status=excluded.status,
                                                                 detail=excluded.detail,
                                                                 checked_at=excluded.checked_at,
                                                                 attempts=excluded.attempts,
                                                                 next_attempt=excluded.next_attempt''',
                         (url, 'retry' if attempts < 5 else 'suspended', str(error)[:2000],
                          time.time(), attempts, deadline if attempts < 5 else None))
        if getattr(error, 'blocked', False):
            self.cur.execute("""INSERT INTO crawl_state
                                VALUES ('pause_until', ?)
                                ON CONFLICT(key) DO UPDATE SET value=MAX(value, excluded.value)""", (deadline,))
        self.commit()

    def next_audio_size_retry(self):
        row = self.cur.execute("SELECT MIN(next_attempt) FROM audio_sizes WHERE status='retry'").fetchone()
        return max(row[0], self.pause_until()) if row[0] is not None else None

    def audio_size_summary(self):
        rows = self.cur.execute('''SELECT COALESCE(s.status, 'pending'),
                                          COUNT(*),
                                          COALESCE(SUM(CASE WHEN s.status = 'known' THEN s.size_bytes ELSE 0 END), 0)
                                   FROM (SELECT DISTINCT audio_url
                                         FROM audios
                                         WHERE audio_url IS NOT NULL
                                           AND TRIM(audio_url) != '') a
                                            LEFT JOIN audio_sizes s ON a.audio_url = s.audio_url
                                   GROUP BY s.status''').fetchall()
        counts = {status: count for status, count, _ in rows}
        return dict(total=sum(counts.values()), known=counts.get('known', 0),
                    known_bytes=sum(size for _, _, size in rows), statuses=counts)

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

    def retry_ready(self, kind, url):
        row = self.cur.execute('SELECT next_attempt FROM retry_schedule WHERE kind=? AND url=?', (kind, url)).fetchone()
        return row is None or (row[0] is not None and row[0] <= time.time())

    def clear_retry(self, kind, url):
        self.cur.execute('DELETE FROM retry_schedule WHERE kind=? AND url=?', (kind, url))

    def pause_until(self):
        row = self.cur.execute("SELECT value FROM crawl_state WHERE key='pause_until'").fetchone()
        return row[0] if row else 0

    def defer(self, kind, url, error, delay=60, blocked=False):
        row = self.cur.execute('SELECT attempts FROM retry_schedule WHERE kind=? AND url=?', (kind, url)).fetchone()
        attempts = row[0] + 1 if row else 1
        # Cinq tentatives réelles maximum par URL et par étape ; pas de suppression.
        seconds = max(delay, min(3600, delay * 2 ** min(attempts - 1, 10)))
        deadline = time.time() + seconds
        next_attempt = deadline if attempts < 5 else None
        self.cur.execute("""
                         INSERT INTO retry_schedule(kind, url, attempts, next_attempt, error)
                         VALUES (?, ?, ?, ?, ?)
                         ON CONFLICT(kind,url) DO UPDATE SET attempts=excluded.attempts,
                                                             next_attempt=excluded.next_attempt,
                                                             error=excluded.error
                         """, (kind, url, attempts, next_attempt, str(error)[:2000]))
        if blocked:
            self.cur.execute(
                "INSERT INTO crawl_state(key,value) VALUES ('pause_until',?) ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)",
                (deadline,))
        self.commit()
        return next_attempt

    def next_retry(self):
        row = self.cur.execute('SELECT MIN(next_attempt) FROM retry_schedule WHERE next_attempt IS NOT NULL').fetchone()
        return max(row[0], self.pause_until()) if row and row[0] is not None else None

    def suspended_count(self):
        return self.cur.execute('SELECT COUNT(*) FROM retry_schedule WHERE next_attempt IS NULL').fetchone()[0]
