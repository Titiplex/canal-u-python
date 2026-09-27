import contextlib
import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from requests.structures import CaseInsensitiveDict

import measure_audio_sizes
from modules import audio_sizes, crawling
from modules.db_manager import SQLManager


class Response:
    def __init__(self, status=200, headers=None, url='https://example.test/audio'):
        self.status_code, self.url = status, url
        self.headers = CaseInsensitiveDict(headers or {})
        self.closed = False

    @property
    def content(self):
        raise AssertionError('Le corps ne doit jamais être lu')

    @property
    def text(self):
        raise AssertionError('Le corps ne doit jamais être lu')

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class AudioSizeTests(unittest.TestCase):
    def tearDown(self):
        crawling.close()

    def probe(self, *responses):
        with patch.object(audio_sizes, '_request', side_effect=responses):
            result = audio_sizes.probe('https://example.test/audio')
        self.assertTrue(all(r.closed for r in responses))
        return result

    def test_head_content_length(self):
        result = self.probe(Response(headers={'Content-Type': 'audio/mpeg', 'Content-Length': '12345'}))
        self.assertEqual(result['size_bytes'], 12345)
        self.assertEqual(result['method'], 'HEAD')

    def test_range_total_not_single_byte_length(self):
        result = self.probe(Response(405), Response(206, {'Content-Type': 'audio/mp3',
                                                          'Content-Length': '1', 'Content-Range': 'bytes 0-0/5000'}))
        self.assertEqual(result['size_bytes'], 5000)
        self.assertEqual(result['method'], 'GET')

    def test_server_ignores_range_body_stays_unread(self):
        result = self.probe(Response(), Response(
            headers={'Content-Type': 'application/octet-stream', 'Content-Length': '700000000'}))
        self.assertEqual(result['size_bytes'], 700000000)

    def test_bad_or_unknown_range_and_compressed_response(self):
        for headers in ({'Content-Range': 'bytes 0-0/*'}, {'Content-Range': 'bytes 0-0/0'},
                        {'Content-Range': 'bytes 2-2/5000'}, {'Content-Encoding': 'gzip'}):
            result = self.probe(Response(),
                                Response(206, {'Content-Type': 'audio/mpeg', 'Content-Length': '1', **headers}))
            self.assertIsNone(result['size_bytes'])
            self.assertEqual(result['status'], 'unknown')

    def test_html_is_not_counted_and_temporary_errors_retry(self):
        for responses in [(Response(429, {'Retry-After': '600'}),), (Response(500),),
                          (Response(), Response(200, {'Content-Type': 'text/html', 'Content-Length': '100'}))]:
            with patch.object(audio_sizes, '_request', side_effect=responses), self.assertRaises(crawling.RetryLater):
                audio_sizes.probe('https://example.test/audio')
            self.assertTrue(all(r.closed for r in responses))

    def test_missing_after_head_fallback(self):
        self.assertEqual(self.probe(Response(404), Response(410))['status'], 'missing')

    def test_redirects_are_paced_and_not_consumed(self):
        crawling.configure(0)
        redirect = Response(302, {'Location': '/file.mp3'})
        final = Response(headers={'Content-Type': 'audio/mpeg', 'Content-Length': '12'})
        with patch.object(crawling.get_client().session, 'request',
                          side_effect=[redirect, final]) as request, patch.object(crawling._gate, 'pace') as pace:
            self.assertEqual(audio_sizes.probe('https://example.test/audio')['size_bytes'], 12)
            self.assertEqual(pace.call_count, 2)
            self.assertEqual(request.call_args.args[1], 'https://example.test/file.mp3')
            self.assertTrue(request.call_args.kwargs['stream'])
            self.assertFalse(request.call_args.kwargs['allow_redirects'])
        self.assertTrue(redirect.closed and final.closed)

    def test_sqlite_distinct_urls_unknowns_and_retry_schedule(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(SQLManager(Path(tmp) / 'db')) as db:
            for page, url in [('p1', 'a'), ('p2', 'a'), ('p3', 'b'), ('p4', 'c')]:
                db.create_audio(page, url, '', '', '', '', '')
            db.save_audio_size('a', dict(size_bytes=100, final_url='a', status='known', method='HEAD', detail=''))
            db.save_audio_size('b', dict(size_bytes=None, final_url='b', status='unknown', method='GET', detail=''))
            db.commit()
            self.assertEqual(db.pending_audio_sizes(), ['c'])
            for _ in range(5):
                db.defer_audio_size('c', TimeoutError('timeout'))
            self.assertEqual(db.pending_audio_sizes(), [])
            self.assertIsNone(db.next_audio_size_retry())
            stats = db.audio_size_summary()
            self.assertEqual((stats['total'], stats['known'], stats['known_bytes']), (3, 1, 100))
            self.assertEqual(stats['statuses']['suspended'], 1)

    def test_real_http_parallel_persistence_and_restart(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_HEAD(self):
                calls.append(('HEAD', self.path))
                self.send_response(405 if self.path == '/range' else 200)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Length', '200')
                self.end_headers()

            def do_GET(self):
                calls.append(('GET', self.path))
                assert self.headers['Range'] == 'bytes=0-0'
                self.send_response(206)
                self.send_header('Content-Type', 'audio/mpeg')
                self.send_header('Content-Range', 'bytes 0-0/300')
                self.send_header('Content-Length', '1')
                self.end_headers()
                self.wfile.write(b'a')

        with ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base = f'http://127.0.0.1:{server.server_port}'
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / 'data.db'
                    with contextlib.closing(SQLManager(path)) as db:
                        for page, url in [('p1', '/head'), ('p2', '/head'), ('p3', '/range')]:
                            db.create_audio(page, base + url, '', '', '', '', '')
                        db.commit()
                    with contextlib.redirect_stdout(io.StringIO()):
                        measure_audio_sizes.measure(path, workers=2, interval=0)
                        measure_audio_sizes.measure(path, workers=2, interval=0)
                    self.assertCountEqual(calls, [('HEAD', '/head'), ('HEAD', '/range'), ('GET', '/range')])
                    with contextlib.closing(SQLManager(path)) as db:
                        stats = db.audio_size_summary()
                        self.assertEqual((stats['total'], stats['known_bytes']), (2, 500))
                        self.assertEqual(db.cur.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 3)
            finally:
                server.shutdown()
                thread.join()

    def test_summary_does_not_request_network(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(audio_sizes,
                                                                'probe') as probe, contextlib.redirect_stdout(
                io.StringIO()):
            measure_audio_sizes.measure(Path(tmp) / 'data.db', summary_only=True)
            probe.assert_not_called()
