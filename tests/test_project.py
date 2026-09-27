import contextlib
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from modules import crawling, parsing
from modules.db_manager import SQLManager
from modules.json_manager import JsonManager
from modules.visual.loadingbar import LoadingBar

BASE = 'https://www.canal-u.tv'


def card(url, kind='Conférence'):
    return f'<article class="node--type-page-media node--view-mode-teaser"><div class="field--name-field-type-production">{kind}</div><div class="wrapper-content"><h3><a href="{url}">Titre</a></h3></div></article>'


def page(content='', folder=False):
    return f'<article class="node--view-mode-full node--type-{"dossier" if folder else "page-media"}"><h1>Titre</h1>{content}</article>'


def audio(n):
    return f'<video><source src="/media/{n}/ressource/podcast" type="audio/mp3"></video>'


# Le contexte sqlite3 gère commit/rollback, mais ne ferme PAS la connexion.
# closing() garantit la fermeture avant le nettoyage du dossier temporaire.
class ProjectTests(unittest.TestCase):
    def test_end_to_end_persistence_children_failure_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'data.db')
            urls = {
                crawling.SEARCH_URL + '0': '<div class="search-results">' + card('/folder', 'Dossier') + '</div>',
                BASE + '/folder': page(audio(1) + card('/child') + card('/bad') + card('/empty'), True),
                BASE + '/child': page(audio(2) + audio(3) + '<div id="videos">' + card('/folder') + '</div>'),
                BASE + '/empty': page(),
            }

            def fetch(url):
                if url.endswith('/bad'):
                    raise ConnectionError('panne temporaire')
                return urls[url]

            with patch.object(crawling, 'get_results_count', return_value=1), patch.object(crawling, 'crawl',
                                                                                           side_effect=fetch), contextlib.redirect_stdout(
                io.StringIO()), self.assertLogs(level='ERROR'):
                main.main(measure_sizes=False, db_path=path)
            with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 3)
                self.assertEqual(conn.execute('SELECT url FROM queue').fetchall(), [(BASE + '/bad',)])
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM visited').fetchone()[0], 4)
                self.assertEqual(conn.execute('SELECT lang FROM audios LIMIT 1').fetchone()[0], '')
                # Simuler l'arrivée de l'heure de reprise avant la relance.
                self.assertIsNotNone(conn.execute('SELECT next_attempt FROM retry_schedule').fetchone()[0])
                conn.execute('UPDATE retry_schedule SET next_attempt=0')
            with patch.object(crawling, 'get_results_count', return_value=1), patch.object(crawling, 'crawl',
                                                                                           return_value=page(
                                                                                               audio(
                                                                                                   4))) as fetch2, contextlib.redirect_stdout(
                io.StringIO()):
                main.main(measure_sizes=False, db_path=path)
            fetch2.assert_called_once_with(BASE + '/bad')
            with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 4)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM queue').fetchone()[0], 0)

    def test_rollback_after_partial_insert_keeps_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.db'
            with contextlib.closing(SQLManager(path)) as db:
                db.save_to_queue({BASE + '/one': ''})
                db.commit()
            original = SQLManager.create_audio

            def fail_after_insert(self, *args, **kwargs):
                original(self, *args, **kwargs)
                raise RuntimeError('simulated DB error')

            with patch.object(crawling, 'get_results_count', return_value=0), patch.object(crawling, 'crawl',
                                                                                           return_value=page(
                                                                                               audio(1))), patch.object(
                SQLManager, 'create_audio', fail_after_insert), contextlib.redirect_stdout(
                io.StringIO()), self.assertLogs(level='ERROR'):
                main.main(measure_sizes=False, db_path=path)
            with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM visited').fetchone()[0], 0)
                self.assertEqual(conn.execute('SELECT COUNT(*) FROM queue').fetchone()[0], 1)

    def test_old_database_and_duplicate_inserts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'data.db'
            with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                conn.execute(
                    'CREATE TABLE audios (id INTEGER PRIMARY KEY, url TEXT, audio_url TEXT, file_name TEXT, title TEXT, desc_ TEXT, lang TEXT, cite TEXT, license TEXT)')
                conn.execute("INSERT INTO audios VALUES (1,'old','old.mp3','saved.mp3','Old','','','','')")
            with contextlib.closing(SQLManager(path)) as db:
                data = ('page', 'audio', 'Title', '', 'Français; Occitan', '', '', 'Montpellier', '10.60527/example')
                db.create_audio(*data)
                db.create_audio(*data)
                self.assertEqual(db.save_to_queue({'page': 'Conférence'}), [('page', 'Conférence')])
                self.assertEqual(db.save_to_queue({'page': 'Conférence'}), [])
                db.commit()
                with contextlib.closing(sqlite3.connect(path)) as conn, conn:
                    self.assertEqual(conn.execute('SELECT COUNT(*) FROM audios').fetchone()[0], 2)
                    self.assertEqual(conn.execute('SELECT file_name FROM audios WHERE id=1').fetchone()[0], 'saved.mp3')
                    self.assertEqual(conn.execute("SELECT lieu,doi FROM audios WHERE url='page'").fetchone(),
                                     ('Montpellier', '10.60527/example'))

    def test_count_boundaries(self):
        for total, expected in [(0, 0), (1, 1), (12, 1), (13, 2), (24, 2), (62254, 5188)]:
            with self.subTest(total=total), patch.object(crawling, 'crawl',
                                                         return_value=f'<span id="global-search-results-counter">{total}</span>'):
                self.assertEqual(crawling.get_results_count(), expected)

    def test_parser_metadata_outside_children_and_absolute_links(self):
        own = '<div class="wrapper-langues"><div class="wrapper-langue">Français</div><div class="wrapper-langue">Occitan</div></div><div class="id-doi-datacite"><span class="field__items">10.60527/test</span></div>'
        child = card('/child').replace('</article>', audio(9) + '</article>')
        html = page(own + audio(1), True) + '<div id="videos">' + child + '</div>'
        ok, infos = parsing.parse_collection(html, BASE + '/folder')
        self.assertTrue(ok)
        self.assertEqual(infos['audios'], [BASE + '/media/1/ressource/podcast'])
        self.assertEqual(infos['langues'], ['Français', 'Occitan'])
        self.assertEqual(infos['doi'], '10.60527/test')
        self.assertEqual(infos['pages'], {BASE + '/child': 'Conférence'})
        with self.assertRaises(ValueError):
            parsing.parse_collection('<h1>Access denied</h1>', BASE)

    def test_json_and_zero_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = JsonManager(str(Path(tmp) / 'nested/data.json'))
            manager.save_json({'français': 'éèà'})
            self.assertEqual(manager.get_json(), {'français': 'éèà'})
        with contextlib.redirect_stdout(io.StringIO()) as output:
            LoadingBar(0).print()
        self.assertIn('100.00%', output.getvalue())
        with patch('modules.visual.loadingbar.time.monotonic', side_effect=[0, 90061]), contextlib.redirect_stdout(
                io.StringIO()) as output:
            bar = LoadingBar(2)
            bar.increment()
            bar.print()
        self.assertIn('1j 01:01:01', output.getvalue())


if __name__ == '__main__':
    unittest.main()
