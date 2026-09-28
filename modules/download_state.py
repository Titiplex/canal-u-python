"""État des téléchargements ; toutes les méthodes sont appelées par le coordinateur."""
import time
from pathlib import Path

from modules import downloading


class DownloadState:
    def __init__(self, db, root):
        self.db, self.root = db, Path(root).resolve()
        db.cur.execute('''CREATE TABLE IF NOT EXISTS downloads
                          (
                              output_root  TEXT    NOT NULL,
                              audio_url    TEXT    NOT NULL,
                              status       TEXT    NOT NULL,
                              file_path    TEXT,
                              size_bytes   INTEGER,
                              attempts     INTEGER NOT NULL DEFAULT 0,
                              next_attempt REAL,
                              detail       TEXT,
                              updated_at   REAL    NOT NULL,
                              PRIMARY KEY (output_root, audio_url)
                          )''')
        db.commit()

    def jobs(self):
        grouped = {}
        for url, page, title, lang in self.db.cur.execute('SELECT audio_url,url,title,lang FROM audios ORDER BY id'):
            if not url or not url.strip():
                continue
            job = grouped.setdefault(url, dict(audio_url=url, title=title or 'audio', pages=[], languages=[]))
            if page and page not in job['pages']:
                job['pages'].append(page)
            if lang and lang not in job['languages']:
                job['languages'].append(lang)
        states = {row[0]: row[1:] for row in self.db.cur.execute(
            'SELECT audio_url,status,next_attempt FROM downloads WHERE output_root=?', (str(self.root),))}
        ready = {}
        for url, job in grouped.items():
            status, deadline = states.get(url, ('pending', None))
            if status == 'done':
                if downloading.existing(job, self.root):
                    continue
                # Fichier effacé/incomplet : il faut restaurer cette copie locale.
                self.db.cur.execute('''UPDATE downloads
                                       SET status='pending',
                                           attempts=0,
                                           next_attempt=NULL
                                       WHERE output_root = ?
                                         AND audio_url = ?''', (str(self.root), url))
                self.db.cur.execute("UPDATE audios SET file_name='' WHERE audio_url=?", (url,))
                status = 'pending'
            if status == 'pending' or (status == 'retry' and deadline <= time.time()):
                ready[url] = job
        self.db.commit()
        return ready

    def save(self, url, result):
        path = str(self.root / result['file'])
        self.db.cur.execute('''INSERT INTO downloads
                                   (output_root, audio_url, status, file_path, size_bytes, updated_at)
                               VALUES (?, ?, 'done', ?, ?, ?)
                               ON CONFLICT(output_root,audio_url) DO UPDATE SET status='done',
                                                                                file_path=excluded.file_path,
                                                                                size_bytes=excluded.size_bytes,
                                                                                next_attempt=NULL,
                                                                                detail='',
                                                                                updated_at=excluded.updated_at''',
                            (str(self.root), url, path, result['size_bytes'], time.time()))
        self.db.cur.execute('UPDATE audios SET file_name=? WHERE audio_url=?', (path, url))
        self.db.save_audio_size(url, dict(size_bytes=result['size_bytes'], final_url=result['final_url'],
                                          status='known', method='DOWNLOAD', detail='Fichier téléchargé'))
        self.db.commit()

    def fail(self, url, error):
        row = self.db.cur.execute('SELECT attempts FROM downloads WHERE output_root=? AND audio_url=?',
                                  (str(self.root), url)).fetchone()
        attempts = (row[0] if row else 0) + 1
        terminal = ('missing' if isinstance(error, downloading.MissingAudio) else
                    'invalid' if isinstance(error, downloading.InvalidAudio) else None)
        delay = max(getattr(error, 'delay', 60), min(3600, 60 * 2 ** min(attempts - 1, 10)))
        deadline = time.time() + delay
        status = terminal or ('retry' if attempts < 5 else 'suspended')
        self.db.cur.execute('''INSERT INTO downloads
                               (output_root, audio_url, status, attempts, next_attempt, detail, updated_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)
                               ON CONFLICT(output_root,audio_url) DO UPDATE SET status=excluded.status,
                                                                                attempts=excluded.attempts,
                                                                                next_attempt=excluded.next_attempt,
                                                                                detail=excluded.detail,
                                                                                updated_at=excluded.updated_at''',
                            (str(self.root), url, status, attempts, deadline if status == 'retry' else None,
                             str(error)[:2000], time.time()))
        if getattr(error, 'blocked', False):
            self.db.cur.execute("""INSERT INTO crawl_state
                                   VALUES ('pause_until', ?)
                                   ON CONFLICT(key) DO UPDATE SET value=MAX(value, excluded.value)""", (deadline,))
        self.db.commit()
        return status

    def next_retry(self):
        value = self.db.cur.execute("SELECT MIN(next_attempt) FROM downloads WHERE output_root=? AND status='retry'",
                                    (str(self.root),)).fetchone()[0]
        return max(value, self.db.pause_until()) if value is not None else None

    def reset_failed(self):
        self.db.cur.execute("""UPDATE downloads
                               SET status='pending',
                                   attempts=0,
                                   next_attempt=NULL
                               WHERE output_root = ?
                                 AND status IN ('retry', 'suspended', 'invalid')""", (str(self.root),))
        self.db.commit()

    def report(self):
        rows = self.db.cur.execute('''SELECT status, COUNT(*), COALESCE(SUM(size_bytes), 0)
                                      FROM downloads
                                      WHERE output_root = ?
                                      GROUP BY status''', (str(self.root),)).fetchall()
        counts = {status: count for status, count, _ in rows}
        size = sum(size for status, _, size in rows if status == 'done')
        print(f'Téléchargements : {counts} ; volume enregistré : {size / 1e9:.3f} Go')
        return counts
