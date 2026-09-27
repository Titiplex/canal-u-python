import contextlib
import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from modules import audio_sizes, crawling
from modules.db_manager import SQLManager
from test_project import BASE, page, audio


def known(url):
    return dict(size_bytes=123, final_url=url, method='HEAD', status='known', detail='')


class IntegratedSizeTests(unittest.TestCase):
    def test_size_runs_during_page_stage_not_after_all_pages(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db'
            with contextlib.closing(SQLManager(path)) as db:
                db.save_to_queue({BASE + '/p1': '', BASE + '/p2': ''})
                db.commit()
            events = []

            def fetch(url):
                events.append(('page', url))
                return page(audio(1))

            def probe(url):
                events.append(('size', url))
                return known(url)

            with patch.object(crawling, 'crawl', side_effect=fetch), patch.object(audio_sizes, 'probe',
                                                                                  side_effect=probe), contextlib.redirect_stdout(
                    io.StringIO()):
                main.main(0, path, page_workers=1, interval=0)
            self.assertEqual([kind for kind, _ in events], ['page', 'size', 'page'])
            with contextlib.closing(SQLManager(path)) as db:
                self.assertEqual(db.audio_size_summary()['known_bytes'], 123)
                self.assertEqual(db.cur.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 2)

    def test_parallel_shared_audio_is_only_measured_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db'
            with contextlib.closing(SQLManager(path)) as db:
                db.save_to_queue({BASE + '/p1': '', BASE + '/p2': ''})
                db.commit()
            barrier = threading.Barrier(2)

            def fetch(url):
                barrier.wait(timeout=5)
                return page(audio(1))

            with patch.object(crawling, 'crawl', side_effect=fetch), patch.object(audio_sizes, 'probe',
                                                                                  side_effect=known) as probe, contextlib.redirect_stdout(
                    io.StringIO()):
                main.main(0, path, page_workers=2, interval=0)
            probe.assert_called_once_with(BASE + '/media/1/ressource/podcast')

    def test_failed_size_keeps_metadata_and_restarts_without_page_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db'
            with contextlib.closing(SQLManager(path)) as db:
                db.save_to_queue({BASE + '/p': ''})
                db.commit()
            with patch.object(crawling, 'crawl', return_value=page(audio(1))), patch.object(audio_sizes, 'probe',
                                                                                            side_effect=TimeoutError(
                                                                                                    'timeout')), contextlib.redirect_stdout(
                    io.StringIO()), self.assertLogs(level='ERROR'):
                deadline = main.main(0, path, interval=0)
            self.assertIsNotNone(deadline)
            with contextlib.closing(SQLManager(path)) as db:
                self.assertTrue(db.is_visited(BASE + '/p'))
                self.assertEqual(db.get_queue_size(), 0)
                self.assertEqual(db.cur.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 1)
                db.cur.execute('UPDATE audio_sizes SET next_attempt=0');
                db.commit()
            with patch.object(crawling, 'crawl') as fetch, patch.object(audio_sizes, 'probe',
                                                                        side_effect=known) as probe, contextlib.redirect_stdout(
                    io.StringIO()):
                main.main(0, path, interval=0)
                main.main(0, path, interval=0)
            fetch.assert_not_called()
            probe.assert_called_once()

    def test_blocked_size_keeps_page_success_and_global_pause(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db'
            with contextlib.closing(SQLManager(path)) as db:
                db.save_to_queue({BASE + '/p1': '', BASE + '/p2': ''})
                db.commit()
            with patch.object(crawling, 'crawl', return_value=page(audio(1))) as fetch, patch.object(audio_sizes,
                                                                                                     'probe',
                                                                                                     side_effect=crawling.RetryLater(
                                                                                                             '429',
                                                                                                             delay=600)), contextlib.redirect_stdout(
                    io.StringIO()), self.assertLogs(level='ERROR'):
                self.assertIsNotNone(main.main(0, path, page_workers=1, interval=0))
            fetch.assert_called_once()
            with contextlib.closing(SQLManager(path)) as db:
                self.assertTrue(db.is_visited(BASE + '/p1'))
                self.assertEqual(db.get_queue(), [(BASE + '/p2', '')])
            with patch.object(crawling, 'crawl') as fetch, patch.object(audio_sizes,
                                                                        'probe') as probe, contextlib.redirect_stdout(
                    io.StringIO()):
                main.main(0, path, interval=0)
            fetch.assert_not_called()
            probe.assert_not_called()

    def test_opt_out_preserves_pending_sizes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'db'
            with contextlib.closing(SQLManager(path)) as db:
                db.save_to_queue({BASE + '/p': ''})
                db.commit()
            with patch.object(crawling, 'crawl', return_value=page(audio(1))), patch.object(audio_sizes,
                                                                                            'probe') as probe, contextlib.redirect_stdout(
                    io.StringIO()):
                main.main(0, path, interval=0, measure_sizes=False)
            probe.assert_not_called()
            with contextlib.closing(SQLManager(path)) as db:
                self.assertEqual(db.pending_audio_sizes(), [BASE + '/media/1/ressource/podcast'])
