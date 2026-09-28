import contextlib
import io
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import requests

import download_audios
from modules import crawling, downloading
from modules.db_manager import SQLManager
from modules.download_state import DownloadState

DATA = b'ID3' + b'x' * (downloading.CHUNK_SIZE * 2 + 100)


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'corpus'
        self.calls = []
        self.failures = set()
        self.active = self.peak = 0
        self.lock = threading.Lock()
        self.barrier = None
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                route = self.path
                owner.calls.append((route, self.headers.get('Range'), self.headers.get('If-Range')))
                if route.startswith('/parallel'):
                    owner.barrier.wait(timeout=5)
                if route == '/redirect':
                    self.send_response(302);
                    self.send_header('Location', '/ok')
                    self.end_headers();
                    return
                if route in {'/missing', '/gone', '/block', '/error', '/invalid', '/fake-audio'}:
                    code = {'/missing': 404, '/gone': 410, '/block': 429}.get(route, 200)
                    body = b'The website encountered an unexpected error. Try again later.' if route == '/error' else b'<html>Not an audio</html>'
                    self.send_response(code)
                    self.send_header('Content-Type', 'audio/mpeg' if route == '/fake-audio' else 'text/html')
                    if route == '/block': self.send_header('Retry-After', '600')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers();
                    self.wfile.write(body);
                    return
                if route == '/temporary' and route not in owner.failures:
                    owner.failures.add(route)
                    self.send_response(500);
                    self.end_headers();
                    return
                offset = int((self.headers.get('Range') or 'bytes=0-').split('=')[1].split('-')[0])
                if route == '/ignore': offset = 0
                self.send_response(206 if offset else 200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('ETag', '"new"' if route == '/changed' else '"v1"')
                self.send_header('Content-Length', str(len(DATA) - offset))
                if offset:
                    start = offset + 1 if route == '/bad-range' else offset
                    self.send_header('Content-Range', f'bytes {start}-{len(DATA) - 1}/{len(DATA)}')
                self.end_headers()
                if route == '/interrupt' and route not in owner.failures:
                    owner.failures.add(route)
                    self.wfile.write(DATA[:downloading.CHUNK_SIZE + 500]);
                    self.wfile.flush()
                    self.connection.shutdown(socket.SHUT_RDWR);
                    self.connection.close();
                    return
                try:
                    self.wfile.write(DATA[offset:])
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f'http://127.0.0.1:{self.server.server_port}'
        crawling.configure(0)

    def tearDown(self):
        crawling.close()
        self.server.shutdown();
        self.thread.join();
        self.server.server_close()
        self.tmp.cleanup()

    def job(self, route='/ok', languages=None):
        return dict(audio_url=self.base + route, title='Titre: test / CON ?',
                    pages=[self.base + '/page'], languages=languages or ['Français'])

    def seed_partial(self, job, size=10000):
        base, state, _ = downloading.paths(job, self.root)
        base.parent.mkdir(parents=True, exist_ok=True)
        base.with_suffix('.part').write_bytes(DATA[:size])
        downloading.atomic_json(state, dict(audio_url=job['audio_url'], etag='"v1"',
                                            final_url=job['audio_url'], total=len(DATA)))

    def test_download_redirect_language_and_no_second_request(self):
        job = self.job('/redirect')
        result = downloading.download(job, self.root)
        self.assertTrue(result['file'].startswith('francais/'))
        self.assertEqual((self.root / result['file']).read_bytes(), DATA)
        downloading.download(job, self.root)
        self.assertEqual([call[0] for call in self.calls], ['/redirect', '/ok'])

    def test_resume_after_real_connection_interruption(self):
        job = self.job('/interrupt')
        with self.assertRaises(requests.RequestException):
            downloading.download(job, self.root)
        base, _, _ = downloading.paths(job, self.root)
        offset = base.with_suffix('.part').stat().st_size
        self.assertEqual(offset, downloading.CHUNK_SIZE)
        result = downloading.download(job, self.root)
        self.assertEqual(self.calls[-1][1:], (f'bytes={offset}-', '"v1"'))
        self.assertEqual((self.root / result['file']).read_bytes(), DATA)

    def test_ignore_range_restarts_instead_of_appending(self):
        job = self.job('/ignore');
        self.seed_partial(job)
        result = downloading.download(job, self.root)
        self.assertEqual((self.root / result['file']).read_bytes(), DATA)

    def test_changed_etag_restarts_before_append(self):
        job = self.job('/changed');
        self.seed_partial(job)
        result = downloading.download(job, self.root)
        self.assertIsNone(self.calls[-1][1])
        self.assertEqual((self.root / result['file']).read_bytes(), DATA)

    def test_bad_range_is_not_appended(self):
        job = self.job('/bad-range');
        self.seed_partial(job)
        with self.assertRaises(crawling.RetryLater):
            downloading.download(job, self.root)
        base, _, _ = downloading.paths(job, self.root)
        self.assertEqual(base.with_suffix('.part').read_bytes(), DATA[:10000])

    def test_missing_server_error_and_invalid_audio_are_distinct(self):
        for route, error in [('/missing', downloading.MissingAudio), ('/gone', downloading.MissingAudio),
                             ('/error', crawling.RetryLater), ('/temporary', crawling.RetryLater),
                             ('/block', crawling.RetryLater), ('/invalid', downloading.InvalidAudio),
                             ('/fake-audio', downloading.InvalidAudio)]:
            with self.subTest(route=route), self.assertRaises(error):
                downloading.download(self.job(route), self.root)
        self.assertFalse(list(self.root.rglob('*.mp3')))

    def test_language_union_and_windows_names(self):
        self.assertEqual(downloading.language_folder([]), 'langue_inconnue')
        self.assertEqual(downloading.language_folder(['fr; Français', 'English']), 'multilingue/anglais-francais')
        self.assertEqual(downloading.slug('CON'), 'lang-con')
        self.assertNotIn('..', downloading.language_folder(['../../français']))

    def test_sqlite_dedup_retry_restart_and_deleted_file(self):
        dbpath = Path(self.tmp.name) / 'data.db'
        with contextlib.closing(SQLManager(dbpath)) as db:
            for page, route, lang in [('p1', '/ok', 'fr'), ('p2', '/ok', 'Français'),
                                      ('p3', '/missing', ''), ('p4', '/temporary', 'English')]:
                db.create_audio(page, self.base + route, 'Titre', '', lang, '', '')
            db.commit()
        with contextlib.redirect_stdout(io.StringIO()), self.assertLogs(level='ERROR'):
            download_audios.run(dbpath, self.root, workers=3, interval=0)
        with contextlib.closing(SQLManager(dbpath)) as db:
            statuses = dict(db.cur.execute('SELECT audio_url,status FROM downloads'))
            self.assertEqual(statuses[self.base + '/ok'], 'done')
            self.assertEqual(statuses[self.base + '/missing'], 'missing')
            self.assertEqual(statuses[self.base + '/temporary'], 'retry')
            paths = db.cur.execute('SELECT file_name FROM audios WHERE audio_url=?', (self.base + '/ok',)).fetchall()
            self.assertEqual(paths[0], paths[1])
            self.assertTrue(Path(paths[0][0]).is_file())
            db.cur.execute('UPDATE downloads SET next_attempt=0');
            db.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            download_audios.run(dbpath, self.root, workers=2, interval=0)
            download_audios.run(dbpath, self.root, workers=2, interval=0)
        self.assertEqual([c[0] for c in self.calls].count('/ok'), 1)
        self.assertEqual([c[0] for c in self.calls].count('/temporary'), 2)
        self.assertEqual([c[0] for c in self.calls].count('/missing'), 1)
        Path(paths[0][0]).unlink()
        with contextlib.redirect_stdout(io.StringIO()):
            download_audios.run(dbpath, self.root, interval=0)
        self.assertEqual([c[0] for c in self.calls].count('/ok'), 2)

    def test_completed_file_recovered_if_database_commit_was_missed(self):
        job = self.job();
        downloading.download(job, self.root)
        dbpath = Path(self.tmp.name) / 'db'
        with contextlib.closing(SQLManager(dbpath)) as db:
            db.create_audio(job['pages'][0], job['audio_url'], job['title'], '', 'Français', '', '')
            db.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            download_audios.run(dbpath, self.root, interval=0)
        self.assertEqual(len(self.calls), 1)

    def test_image_challenge_uses_same_session_and_streaming_refresh(self):
        def response(code, body, mime, url):
            r = requests.Response();
            r.status_code = code;
            r.url = url
            r.headers.update({'Content-Type': mime, 'Content-Length': str(len(body))})
            r.raw = io.BytesIO(body)
            return r

        url = 'https://www.canal-u.tv/media/1/ressource/podcast'
        challenge = response(403,
                             b"<meta http-equiv='refresh' content='1'><img src='/sites/default/files/bot_challenge.png?abc'>Challenge...",
                             'text/html', url)
        audio = response(200, DATA, 'audio/mpeg', url)
        image = response(404, b'', 'text/html', url)
        job = self.job();
        job['audio_url'] = url
        with patch.object(downloading, '_request', side_effect=[challenge, audio]) as fetch, patch.object(
                crawling.get_client().session, 'get', return_value=image) as image_get, patch.object(crawling._gate,
                                                                                                     'sleep'):
            result = downloading.download(job, self.root)
        self.assertEqual(result['size_bytes'], len(DATA))
        self.assertEqual(fetch.call_count, 2)
        self.assertIn('bot_challenge.png?abc', image_get.call_args.args[0])

    def test_transfers_overlap_and_sqlite_has_one_writer(self):
        self.barrier = threading.Barrier(2)
        dbpath = Path(self.tmp.name) / 'db'
        with contextlib.closing(SQLManager(dbpath)) as db:
            for i in (1, 2):
                db.create_audio('page', self.base + f'/parallel{i}', 'Test', '', 'fr', '', '')
            db.commit()
        threads = []
        save = DownloadState.save

        def record_save(state, *args):
            threads.append(threading.get_ident())
            return save(state, *args)

        with patch.object(DownloadState, 'save', record_save), contextlib.redirect_stdout(io.StringIO()):
            download_audios.run(dbpath, self.root, workers=2, interval=0)
        self.assertEqual(threads, [threading.get_ident()] * 2)
        self.assertEqual(len(list(self.root.rglob('*.mp3'))), 2)

    def test_block_stops_new_jobs_and_pause_survives_restart(self):
        dbpath = Path(self.tmp.name) / 'db'
        with contextlib.closing(SQLManager(dbpath)) as db:
            for route in ('/block', '/ok'):
                db.create_audio('page', self.base + route, 'Test', '', '', '', '')
            db.commit()
        with contextlib.redirect_stdout(io.StringIO()), self.assertLogs(level='ERROR'):
            deadline = download_audios.run(dbpath, self.root, workers=1, interval=0)
        self.assertIsNotNone(deadline)
        with contextlib.redirect_stdout(io.StringIO()):
            download_audios.run(dbpath, self.root, interval=0)
        self.assertEqual([call[0] for call in self.calls], ['/block'])

    def test_partial_without_strong_etag_restarts(self):
        job = self.job();
        self.seed_partial(job)
        _, checkpoint, _ = downloading.paths(job, self.root)
        state = downloading.read_json(checkpoint);
        state['etag'] = 'W/"weak"'
        downloading.atomic_json(checkpoint, state)
        result = downloading.download(job, self.root)
        self.assertIsNone(self.calls[0][1])
        self.assertEqual((self.root / result['file']).read_bytes(), DATA)
