import contextlib
import io
import sqlite3
import tempfile
import threading
import time
import unittest
from collections import Counter
from pathlib import Path
from unittest.mock import patch

import main
from modules import crawling
from modules.db_manager import SQLManager
from modules.process_lock import DatabaseRunLock
from test_project import BASE, card, page, audio


class ParallelTests(unittest.TestCase):
    def test_real_http_sessions_with_local_server_and_sqlite(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.parse import urlsplit, parse_qs
        state = {'active': 0, 'peak': 0}
        lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                with lock:
                    state['active'] += 1
                    state['peak'] = max(state['peak'], state['active'])
                try:
                    time.sleep(0.08)
                    split = urlsplit(self.path)
                    if split.path == '/recherche':
                        n = int(parse_qs(split.query)['page'][0]) + 1
                        body = '<span id="global-search-results-counter">13</span><div class="search-results">' + card(
                            f'/p{n}') + '</div>'
                    elif split.path in ('/p1', '/p2'):
                        body = page(audio(int(split.path[-1])) + card('/child'), True)
                    else:
                        body = page(audio(3))
                    data = body.encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'text/html; charset=utf-8')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                finally:
                    with lock:
                        state['active'] -= 1

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02})
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / 'data.db'
                search = f'http://127.0.0.1:{server.server_port}/recherche?page='
                with patch.object(crawling, 'SEARCH_URL', search), patch.object(crawling, 'USE_BROWSER_FALLBACK',
                                                                                False), contextlib.redirect_stdout(
                    io.StringIO()):
                    main.main(measure_sizes=False, db_path=path, search_workers=2, page_workers=2, interval=0.01)
                with contextlib.closing(sqlite3.connect(path)) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 3)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM queue').fetchone()[0], 0)
            self.assertEqual(state['peak'], 2)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_both_stages_overlap_single_writer_dedup_cycles_and_session_cleanup(self):
        owner = threading.get_ident()
        sql_threads = set()
        calls = Counter()
        search_barrier = threading.Barrier(2)
        page_barrier = threading.Barrier(2)
        lock = threading.Lock()
        clients = []

        class CheckedDB(SQLManager):
            def __init__(self, path):
                super().__init__(path)
                self.conn.set_trace_callback(lambda _: sql_threads.add(threading.get_ident()))

        class Session:
            def __init__(self):
                self.owner = threading.get_ident()
                self.closed_by = None
                self.headers = {}
                clients.append(self)

            def close(self):
                self.closed_by = threading.get_ident()

        def fetch(url):
            client = crawling.get_client()
            self.assertEqual(client.session.owner, threading.get_ident())
            with lock:
                calls[url] += 1
            if url.startswith(crawling.SEARCH_URL):
                search_barrier.wait(timeout=3)
                n = int(url.rsplit('=', 1)[1]) + 1
                return '<div class="search-results">' + card(f'/p{n}') + '</div>'
            if url in (BASE + '/p1', BASE + '/p2'):
                page_barrier.wait(timeout=3)
                n = int(url[-1])
                return page(audio(n) + card('/shared') + card('/p1') + card('/p2'), True)
            return page(audio(3) + card('/p1'), True)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.db'
            with patch.object(main.dbm, 'SQLManager', CheckedDB), patch.object(crawling.requests, 'Session',
                                                                               Session), patch.object(crawling,
                                                                                                      'get_results_count',
                                                                                                      return_value=2), patch.object(
                crawling, 'crawl', side_effect=fetch), contextlib.redirect_stdout(io.StringIO()):
                main.main(measure_sizes=False, db_path=path, search_workers=2, page_workers=2)
            with contextlib.closing(sqlite3.connect(path)) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 3)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM visited').fetchone()[0], 5)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM queue').fetchone()[0], 0)
        self.assertEqual(sql_threads, {owner})
        self.assertEqual(len(calls), 5)
        self.assertTrue(all(n == 1 for n in calls.values()))
        self.assertEqual(len(clients), 4)
        self.assertTrue(all(c.owner == c.closed_by and c.owner != owner for c in clients))

    def test_block_stops_dispatch_but_saves_other_inflight_success(self):
        barrier = threading.Barrier(2)
        fetched = []

        def fetch(url):
            fetched.append(url)
            barrier.wait(timeout=3)
            if url.endswith('/a'):
                raise crawling.RetryLater('429', delay=900)
            self.assertTrue(crawling._gate.stopped.wait(3))
            return page(audio(1))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.db'
            with contextlib.closing(SQLManager(path)) as db:
                db.save_to_queue({BASE + '/a': '', BASE + '/b': '', BASE + '/c': ''})
                db.commit()
            with patch.object(crawling, 'get_results_count', return_value=0), patch.object(crawling, 'crawl',
                                                                                           side_effect=fetch), contextlib.redirect_stdout(
                io.StringIO()), self.assertLogs(level='ERROR'):
                main.main(measure_sizes=False, db_path=path, page_workers=2)
            with contextlib.closing(SQLManager(path)) as db:
                self.assertEqual({u for u, _ in db.get_queue()}, {BASE + '/a', BASE + '/c'})
                self.assertTrue(db.is_visited(BASE + '/b'))
                self.assertGreater(db.pause_until(), time.time())
            self.assertEqual(set(fetched), {BASE + '/a', BASE + '/b'})

    def test_global_spacing_across_threads(self):
        gate = crawling.RequestGate(0.03)
        barrier = threading.Barrier(4)
        starts = []

        def worker():
            barrier.wait(timeout=3)
            gate.pace()
            starts.append(time.monotonic())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertEqual(len(starts), 4)
        starts.sort()
        self.assertTrue(all(b - a >= 0.025 for a, b in zip(starts, starts[1:])), starts)

    def test_stop_interrupts_pacing_wait(self):
        gate = crawling.RequestGate(30)
        gate.pace()
        result = []
        ready = threading.Event()

        def wait():
            ready.set()
            try:
                gate.pace()
            except crawling.CrawlCancelled:
                result.append('cancelled')

        thread = threading.Thread(target=wait)
        thread.start()
        ready.wait(2)
        gate.stopped.set()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, ['cancelled'])

    def test_database_lock_refuses_second_instance_and_releases(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.db'
            with DatabaseRunLock(path):
                with self.assertRaises(RuntimeError):
                    with DatabaseRunLock(path):
                        pass
            with DatabaseRunLock(path):
                pass


if __name__ == '__main__':
    unittest.main()
