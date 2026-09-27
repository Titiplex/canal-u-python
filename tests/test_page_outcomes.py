import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import main
from modules import crawling, parsing
from modules.db_manager import SQLManager
from test_project import card, page, audio, BASE


class PageOutcomeTests(unittest.TestCase):
    def test_collection_div_taxonomy_and_main(self):
        for kind in ('node--view-mode-full', 'taxonomy-term', ''):
            html = f'<html><body><main><h1>Collection</h1><div class="{kind}"></div><section id="videos">{card("/child")}</section></main></body></html>'
            found, meta = parsing.parse_collection(html, BASE + '/collection')
            self.assertTrue(found)
            self.assertEqual(meta['titre'], 'Collection')
            self.assertEqual(meta['pages'], {BASE + '/child': 'Conférence'})

    def test_main_heading_sections_exclude_suggestions_and_people(self):
        html = '<main><h1>Collection</h1><h2>Vidéos</h2><div>' + card('/child') + '</div><h2>Suggestions</h2>' + card(
            '/unrelated') + '</main>'
        self.assertEqual(parsing.parse_collection(html, BASE)[1]['pages'], {BASE + '/child': 'Conférence'})

    def test_div_cards_media_not_assigned_to_parent_and_pagination(self):
        child = card('/child').replace('article', 'div').replace('</h3>', '</h3>' + audio(9))
        html = page(audio(1),
                    True) + '<section id="videos">' + child + '<li class="pager__item--next"><a href="?page=1">Suite</a></li></section>'
        meta = parsing.parse_collection(html, BASE + '/collection')[1]
        self.assertEqual(meta['audios'], [BASE + '/media/1/ressource/podcast'])
        self.assertEqual(meta['pages'], {BASE + '/child': 'Conférence', BASE + '/collection?page=1': 'pagination'})

    def test_loaded_empty_vs_incomplete_and_unknown(self):
        self.assertEqual(parsing.parse_collection(page())[1]['status'], 'sans_contenu')
        for html in ('<h1>Access denied</h1>', '<html><main><h1>Partiel</h1>', '<title>Challenge...</title>'):
            with self.subTest(html=html), self.assertRaises(parsing.IncompletePage):
                parsing.parse_collection(html)
        with self.assertRaises(parsing.UnrecognizedPage):
            parsing.parse_collection(page('<iframe src="/player/1"></iframe>'))

    def test_terminal_pages_removed_retryable_retained_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            dbpath = Path(tmp) / 'data.db'
            with contextlib.closing(SQLManager(dbpath)) as db:
                db.save_to_queue(
                    {BASE + '/' + name: '' for name in ('empty', 'timeout', 'missing', 'unknown', 'collection')})
                db.defer('page', BASE + '/collection',
                         'Page Canal-U non reconnue ou incomplète ; elle reste à reprendre')

            def fetch(url):
                if url.endswith('/timeout'):
                    raise TimeoutError('timeout')
                if url.endswith('/missing'):
                    raise crawling.PermanentHTTPError('HTTP 404')
                if url.endswith('/unknown'):
                    return page('<iframe src="/player/1"></iframe>')
                if url.endswith('/collection'):
                    return '<main><h1>Collection</h1><div id="videos">' + card('/empty') + '</div></main>'
                return page()

            with patch.object(crawling, 'crawl', side_effect=fetch), patch.object(crawling,
                                                                                  'get_results_count') as count, contextlib.redirect_stdout(
                io.StringIO()), self.assertLogs(level='WARNING'):
                main.main(0, dbpath, measure_sizes=False, interval=0)
                count.assert_not_called()
            with contextlib.closing(SQLManager(dbpath)) as db:
                self.assertEqual(db.get_queue(), [(BASE + '/timeout', '')])
                statuses = dict(db.cur.execute('SELECT url,status FROM page_outcomes'))
                self.assertEqual(statuses[BASE + '/empty'], 'sans_contenu')
                self.assertEqual(statuses[BASE + '/unknown'], 'a_verifier')
                self.assertEqual(statuses[BASE + '/missing'], 'introuvable')
                self.assertEqual(statuses[BASE + '/collection'], 'exploitable')
                self.assertEqual(db.cur.execute('SELECT url FROM retry_schedule').fetchall(), [(BASE + '/timeout',)])
                db.cur.execute('UPDATE retry_schedule SET next_attempt=0');
                db.commit()
            self.assertEqual(len(list((Path(tmp) / 'pages_a_verifier').glob('*.html'))), 1)
            with patch.object(crawling, 'crawl', return_value=page()) as fetch_again, contextlib.redirect_stdout(
                    io.StringIO()):
                main.main(0, dbpath, measure_sizes=False, interval=0)
            fetch_again.assert_called_once_with(BASE + '/timeout')

    def test_upgrade_only_old_parser_errors_once_including_suspended(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(SQLManager(Path(tmp) / 'db')) as db:
            db.save_to_queue({'old': '', 'network': ''})
            for _ in range(5):
                db.defer('page', 'old', 'Page Canal-U non reconnue ou incomplète ; elle reste à reprendre')
            db.defer('page', 'network', 'timeout')
            self.assertEqual(db.upgrade_parser(), 1)
            self.assertTrue(db.retry_ready('page', 'old'))
            self.assertFalse(db.retry_ready('page', 'network'))
            self.assertEqual(db.upgrade_parser(), 0)

    def test_http_404_terminal_500_retry_without_stopping_everyone(self):
        for status, exception in [(404, crawling.PermanentHTTPError), (410, crawling.PermanentHTTPError),
                                  (500, crawling.RetryLater)]:
            crawling.configure(0)
            with contextlib.closing(crawling.Client()) as client, patch.object(client, '_image_challenge',
                                                                               side_effect=lambda r: r):
                response = Mock(status_code=status, text='Erreur serveur', headers={}, apparent_encoding='utf-8')
                with patch.object(client.session, 'get', return_value=response), self.assertRaises(exception):
                    client.crawl(BASE)
                self.assertFalse(crawling.stopped())
