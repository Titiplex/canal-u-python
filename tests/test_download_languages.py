import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from download_audios import build_parser
from modules.db_manager import SQLManager
from modules.download_state import DownloadState


class LanguageFilterTests(unittest.TestCase):
    def test_cli_empty_multiple_and_repeated_arguments(self):
        parser = build_parser()
        self.assertIsNone(parser.parse_args([]).languages)
        self.assertEqual(parser.parse_args(['--languages', 'Français', 'Anglais', '']).languages,
                         ['Français', 'Anglais', ''])
        self.assertEqual(parser.parse_args(['--lang=Français', '--lang=']).languages, ['Français', ''])

    def test_selection_dedup_metadata_and_retry_scope(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(SQLManager(Path(tmp) / 'data.db')) as db:
            for i, (url, lang) in enumerate([
                    ('fr', 'Français'), ('en', 'Anglais'), ('blank', ''), ('null', None),
                    ('spaces', '   '), ('shared', 'Français'), ('shared', 'Anglais'),
                    ('multi', 'Français; Anglais')]):
                db.create_audio(str(i), url, 'Titre', '', lang, '', '')
            db.commit()
            root = Path(tmp) / 'output'
            state = DownloadState(db, root, ['Français', ''])
            self.assertEqual(set(state.jobs()), {'fr', 'blank', 'null', 'spaces', 'shared'})
            self.assertEqual(state.jobs()['shared']['languages'], ['Français', 'Anglais'])
            self.assertEqual(len(DownloadState(db, root).jobs()), 7)
            self.assertEqual(set(DownloadState(db, root, ['Français; Anglais']).jobs()), {'multi'})
            all_state = DownloadState(db, root)
            all_state.fail('en', RuntimeError('temporary'))
            self.assertIsNone(state.next_retry())
            state.reset_failed()
            self.assertEqual(db.cur.execute("SELECT status FROM downloads WHERE audio_url='en'").fetchone()[0], 'retry')
            all_state.fail('fr', RuntimeError('temporary'))
            self.assertIsNotNone(state.next_retry())
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(state.report(), {'retry': 1})
            state.reset_failed()
            self.assertIn('fr', state.jobs())
            self.assertNotIn('en', state.jobs())


if __name__ == '__main__':
    unittest.main()
