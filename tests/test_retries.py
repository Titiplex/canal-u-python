import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from modules import crawling
from modules.db_manager import SQLManager


class RetryTests(unittest.TestCase):
    def setUp(self):
        crawling._use_browser = False
        crawling._session_closed = False

    def response(self, status=200, text='<h1>ok</h1>', headers=None):
        return Mock(status_code=status, text=text, headers=headers or {}, apparent_encoding='utf-8')

    def test_browser_is_lazy_and_reused_after_challenge(self):
        session = Mock()
        session.get.side_effect = [self.response(), self.response(403, '<title>Challenge...</title>')]
        with patch.object(crawling, 'session', session), patch.object(crawling, '_pace'), patch.object(crawling,
                                                                                                       '_browser_fetch',
                                                                                                       return_value='resolved') as browser, contextlib.redirect_stdout(
                io.StringIO()):
            self.assertEqual(crawling.crawl('https://example.test/one'), '<h1>ok</h1>')
            browser.assert_not_called()
            self.assertEqual(crawling.crawl('https://example.test/two'), 'resolved')
            self.assertEqual(crawling.crawl('https://example.test/three'), 'resolved')
        self.assertEqual(session.get.call_count, 2)
        self.assertEqual(browser.call_count, 2)

    def test_retry_after_and_plain_403_do_not_launch_browser(self):
        for response in [self.response(429, 'Challenge', {'Retry-After': '7200'}), self.response(403, 'Forbidden')]:
            crawling._use_browser = False
            with patch.object(crawling, 'session') as session, patch.object(crawling, '_pace'), patch.object(crawling,
                                                                                                             '_browser_fetch') as browser:
                session.get.return_value = response
                with self.assertRaises(crawling.RetryLater) as error:
                    crawling.crawl('https://example.test')
                browser.assert_not_called()
                if response.status_code == 429:
                    self.assertGreaterEqual(error.exception.delay, 7200)

    def test_challenge_detection_and_http_date(self):
        self.assertTrue(crawling._challenge('<title>Challenge...</title>'))
        self.assertFalse(
            crawling._challenge('<article class="node--view-mode-full"><h1>The AI challenge</h1></article>'))
        from datetime import datetime, timezone, timedelta
        from email.utils import format_datetime
        date = format_datetime(datetime.now(timezone.utc) + timedelta(hours=2))
        self.assertGreaterEqual(crawling._retry_delay(date), 7198)

    def test_persistent_schedule_escalation_and_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.db'
            with contextlib.closing(SQLManager(path)) as db, patch('modules.db_manager.time.time', return_value=1000):
                first = db.defer('search', 'u', '403', 300, True)
                self.assertEqual(first, 1300)
                self.assertFalse(db.retry_ready('search', 'u'))
            with contextlib.closing(SQLManager(path)) as db, patch('modules.db_manager.time.time', return_value=1301):
                self.assertTrue(db.retry_ready('search', 'u'))
                self.assertEqual(db.defer('search', 'u', '403', 300, True), 1901)
                for _ in range(3):
                    result = db.defer('search', 'u', '403', 300, True)
                self.assertIsNone(result)
                self.assertFalse(db.retry_ready('search', 'u'))
                self.assertEqual(db.suspended_count(), 1)

    def test_global_pause_prevents_all_network_calls_on_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.db'
            with patch.object(crawling, 'get_results_count',
                              side_effect=crawling.RetryLater('Challenge', 900)), contextlib.redirect_stdout(
                    io.StringIO()), self.assertLogs(level='ERROR'):
                deadline = main.main(db_path=path)
            self.assertGreater(deadline, time.time())
            with patch.object(crawling, 'get_results_count') as count, patch.object(crawling,
                                                                                    'crawl') as fetch, contextlib.redirect_stdout(
                    io.StringIO()):
                self.assertEqual(main.main(db_path=path), deadline)
                count.assert_not_called()
                fetch.assert_not_called()


if __name__ == '__main__':
    unittest.main()
