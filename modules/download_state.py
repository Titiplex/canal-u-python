"""État des téléchargements ; toutes les méthodes sont appelées par le coordinateur."""
import csv
import os
import time
from pathlib import Path

from modules import downloading


class DownloadState:
    def __init__(self, db, root, languages=None):
        self.db, self.root = db, Path(root).resolve()
        # None = toutes les langues ; [''] = uniquement les non-libellés.
        self.languages = None if languages is None else list(dict.fromkeys(v.strip() for v in languages))
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

    def _language_filter(self, url_column):
        if self.languages is None:
            return '', ()
        placeholders = ','.join('?' for _ in self.languages)
        return (f' AND EXISTS (SELECT 1 FROM audios AS language_source '
                f'WHERE language_source.audio_url={url_column} '
                f"AND TRIM(COALESCE(language_source.lang, '')) IN ({placeholders}))",
                tuple(self.languages))

    def jobs(self):
        grouped = {}
        clause, params = self._language_filter('audios.audio_url')
        for url, page, title, lang in self.db.cur.execute(
                'SELECT audio_url,url,title,lang FROM audios WHERE 1=1' + clause + ' ORDER BY id', params):
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
        self.db.upsert('downloads', dict(output_root=str(self.root), audio_url=url),
                       dict(status='done', file_path=path, size_bytes=result['size_bytes'], updated_at=time.time()),
                       update_only=dict(next_attempt=None, detail=''))
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
        self.db.upsert('downloads', dict(output_root=str(self.root), audio_url=url),
                       dict(status=status, attempts=attempts, next_attempt=deadline if status == 'retry' else None,
                            detail=str(error)[:2000], updated_at=time.time()))
        if getattr(error, 'blocked', False):
            self.db.extend_pause(deadline)
        self.db.commit()
        return status

    def next_retry(self):
        clause, params = self._language_filter('downloads.audio_url')
        value = \
        self.db.cur.execute("SELECT MIN(next_attempt) FROM downloads WHERE output_root=? AND status='retry'" + clause,
                            (str(self.root), *params)).fetchone()[0]
        return max(value, self.db.pause_until()) if value is not None else None

    def reset_failed(self):
        clause, params = self._language_filter('downloads.audio_url')
        self.db.cur.execute("""UPDATE downloads
                               SET status='pending',
                                   attempts=0,
                                   next_attempt=NULL
        WHERE output_root = ?
          AND status IN ('retry', 'suspended', 'invalid')""" + clause, (str(self.root), *params))
        self.db.commit()

    def report(self):
        clause, params = self._language_filter('downloads.audio_url')
        rows = self.db.cur.execute('''SELECT status, COUNT(*), COALESCE(SUM(size_bytes), 0)
                                      FROM downloads
                                      WHERE output_root = ?
                                      ''' + clause + ' GROUP BY status', (str(self.root), *params)).fetchall()
        counts = {status: count for status, count, _ in rows}
        size = sum(size for status, _, size in rows if status == 'done')
        print(f'Téléchargements : {counts} ; volume enregistré : {size / 1e9:.3f} Go')
        return counts

    def export_errors(self, path):
        """Rapport remplaçable : aucune modification des URL ni suppression d'audio."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        clause, params = self._language_filter('downloads.audio_url')
        rows = self.db.cur.execute('''SELECT audio_url, status, attempts, next_attempt, detail,
                                      (SELECT GROUP_CONCAT(DISTINCT url) FROM audios
                                       WHERE audios.audio_url=downloads.audio_url)
                                      FROM downloads WHERE output_root=? AND status!='done'
                                      ''' + clause + ' ORDER BY status, audio_url', (str(self.root), *params))
        temp = path.with_suffix(path.suffix + '.tmp')
        with temp.open('w', newline='', encoding='utf-8-sig') as stream:
            writer = csv.writer(stream)
            writer.writerow(['audio_url', 'status', 'attempts', 'next_attempt_unix', 'detail', 'pages'])
            writer.writerows(rows)
        os.replace(temp, path)
