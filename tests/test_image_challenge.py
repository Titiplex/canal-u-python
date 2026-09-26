import contextlib
import io
import unittest
from unittest.mock import Mock, patch

from modules import crawling

URL = crawling.SEARCH_URL + '3'
CHALLENGE = "<!DOCTYPE html><html><head><meta http-equiv='refresh' content='1'></head><body><img src='/sites/default/files/bot_challenge.png?test-token' />Challenge...</body></html>"
REAL = '<span id="global-search-results-counter">62264</span>'


def response(status=200, html=REAL, headers=None, url=URL):
    return Mock(status_code=status, text=html, headers=headers or {}, url=url,
                apparent_encoding='utf-8')


class ImageChallengeTests(unittest.TestCase):
    def setUp(self):
        crawling.configure()
        self.client = crawling.Client()
        self.addCleanup(self.client.close)

    def test_image_404_then_refresh_success_without_browser(self):
        client = Mock()
        image = response(404, 'Not found')
        client.get.side_effect = [response(403, CHALLENGE), image, response()]
        with patch.object(self.client, 'session', client), patch.object(crawling._gate, 'pace'), patch.object(
                crawling._gate, 'sleep') as sleep, patch.object(crawling, 'USE_BROWSER_FALLBACK', False), patch.object(
                self.client, '_browser_fetch') as browser, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.client.crawl(URL), REAL)
        self.assertEqual([c.args[0] for c in client.get.call_args_list],
                         [URL, 'https://www.canal-u.tv/sites/default/files/bot_challenge.png?test-token', URL])
        self.assertFalse(client.get.call_args_list[1].kwargs['allow_redirects'])
        sleep.assert_called_once_with(1.0)
        image.close.assert_called_once()
        browser.assert_not_called()

    def test_no_image_request_for_unknown_or_external_model(self):
        cases = [
            CHALLENGE.replace("content='1'", "content='1;url=https://external.test/'"),
            CHALLENGE.replace("src='/sites", "src='https://external.test/sites"),
            CHALLENGE.replace('</body>', '<script>something()</script></body>'),
            CHALLENGE.replace("content='1'", "content='9999'"),
        ]
        with patch.object(self.client, 'session') as client:
            for html in cases:
                r = response(403, html)
                self.assertIs(self.client._image_challenge(r), r)
            r = response(403, CHALLENGE, url='https://external.test/recherche')
            self.assertIs(self.client._image_challenge(r), r)
        client.get.assert_not_called()

    def test_refused_page_retry_after_does_not_load_image(self):
        client = Mock()
        client.get.return_value = response(403, CHALLENGE, {'Retry-After': '7200'})
        with patch.object(self.client, 'session', client), patch.object(crawling._gate, 'pace'), patch.object(
                self.client, '_browser_fetch') as browser:
            with self.assertRaises(crawling.RetryLater) as error:
                self.client.crawl(URL)
        self.assertGreaterEqual(error.exception.delay, 7200)
        self.assertEqual(client.get.call_count, 1)
        browser.assert_not_called()

    def test_image_429_stops_before_refresh(self):
        client = Mock()
        client.get.side_effect = [response(403, CHALLENGE), response(429, '', {'Retry-After': '7200'})]
        with patch.object(self.client, 'session', client), patch.object(crawling._gate, 'pace'), patch.object(
                crawling._gate, 'sleep') as sleep, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(crawling.RetryLater) as error:
                self.client.crawl(URL)
        self.assertGreaterEqual(error.exception.delay, 7200)
        self.assertEqual(client.get.call_count, 2)
        sleep.assert_not_called()

    def test_persistent_challenge_has_only_one_image_and_one_refresh(self):
        client = Mock()
        client.get.side_effect = [response(403, CHALLENGE), response(404, ''), response(403, CHALLENGE)]
        with patch.object(self.client, 'session', client), patch.object(crawling._gate, 'pace'), patch.object(
                crawling._gate, 'sleep'), patch.object(crawling, 'USE_BROWSER_FALLBACK',
                                                       False), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(crawling.RetryLater):
                self.client.crawl(URL)
        self.assertEqual(client.get.call_count, 3)

    def test_normal_page_uses_one_request(self):
        client = Mock()
        client.get.return_value = response()
        with patch.object(self.client, 'session', client), patch.object(crawling._gate, 'pace'):
            self.assertEqual(self.client.crawl(URL), REAL)
        client.get.assert_called_once()


if __name__ == '__main__':
    unittest.main()
