import contextlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from transcribe import Lang, build_parser, prepare_database, save_transcription, transcribe
from modules.db_manager import SQLManager


class FakeModel:
    device = SimpleNamespace(type='cpu')

    def __init__(self, failures=()):
        self.calls = []
        self.failures = set(failures)

    def transcribe(self, path, **options):
        self.calls.append((path, options))
        if Path(path).name in self.failures:
            raise RuntimeError('Audio invalide')
        return dict(text=' Bonjour éà. ', language='fr',
                    segments=[dict(start=0.0, end=1.5, text=' Bonjour éà.')])


class TranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / 'audios'
        self.root.mkdir()
        self.db_path = Path(self.tmp.name) / 'data.db'
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute('CREATE TABLE audios (id INTEGER PRIMARY KEY, audio_url TEXT, file_name TEXT, title TEXT)')

    def add(self, url, filename, title="Titre d'origine"):
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            return conn.execute('INSERT INTO audios(audio_url,file_name,title) VALUES(?,?,?)',
                                (url, str(filename), title)).lastrowid

    def audio(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b'fake audio')
        return path

    def run_transcribe(self, model, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return transcribe(self.root, db_path=self.db_path, model=model, **kwargs)

    def test_migration_utf8_segments_duplicate_sources_and_resume(self):
        path = self.audio('a.mp3')
        self.add('source', path)
        self.add('source', '')
        self.audio('unregistered.wav')
        (self.root / 'ignore.json').write_text('{}')
        model = FakeModel()
        stats = self.run_transcribe(model, lang=Lang.uk)
        self.assertEqual((stats['done'], stats['rows_saved']), (1, 2))
        self.assertEqual(len(model.calls), 1)
        self.assertIsNone(model.calls[0][1]['language'])
        self.assertFalse(model.calls[0][1]['fp16'])
        self.assertEqual(model.calls[0][1]['task'], 'transcribe')
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            rows = conn.execute('SELECT title,transcription,transcription_segments,transcription_lang,transcribed_at FROM audios').fetchall()
            self.assertEqual(len(rows), 2)
            for title, text, segments, lang, date in rows:
                self.assertEqual((title, text, lang), ("Titre d'origine", 'Bonjour éà.', 'fr'))
                self.assertEqual(json.loads(segments)[0]['end'], 1.5)
                self.assertTrue(date.endswith('+00:00'))
        self.assertEqual(self.run_transcribe(model)['done'], 0)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(self.run_transcribe(model, overwrite=True)['rows_saved'], 2)

    def test_preserve_existing_text_when_new_duplicate_added(self):
        path = self.audio('a.wav')
        first = self.add('same', path)
        self.run_transcribe(FakeModel())
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute('UPDATE audios SET transcription=? WHERE id=?', ('Texte corrigé', first))
        self.add('same', '')
        self.assertEqual(self.run_transcribe(FakeModel())['rows_saved'], 1)
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.assertEqual(conn.execute('SELECT transcription FROM audios WHERE id=?', (first,)).fetchone()[0], 'Texte corrigé')

    def test_moved_windows_paths_and_ambiguous_basenames(self):
        self.audio('moved.mp3')
        self.add('moved', r'C:\old\audios\moved.mp3')
        self.audio('duplicate.mp3')
        self.add('one', r'C:\one\duplicate.mp3')
        self.add('two', r'C:\two\duplicate.mp3')
        self.audio('nested/x.wav')
        self.audio('other/x.wav')
        self.add('x', r'C:\old\x.wav')
        model = FakeModel()
        stats = self.run_transcribe(model)
        self.assertEqual(stats['done'], 1)
        self.assertEqual(stats['ambiguous'], 3)
        self.assertEqual(Path(model.calls[0][0]).name, 'moved.mp3')

    def test_exact_paths_disambiguate_equal_names_and_reject_shared_file(self):
        first = self.audio('one/a.mp3')
        second = self.audio('two/a.mp3')
        self.add('one', first)
        self.add('two', second)
        model = FakeModel()
        self.assertEqual(self.run_transcribe(model)['done'], 2)
        shared = self.audio('shared.mp3')
        self.add('wrong1', shared)
        self.add('wrong2', shared)
        stats = self.run_transcribe(FakeModel())
        self.assertEqual((stats['done'], stats['ambiguous']), (0, 2))

    def test_downloads_fallback_failure_retry_and_limit(self):
        bad = self.audio('bad.mp3')
        good = self.audio('good.mp3')
        self.add('bad', bad)
        self.add('good', '')
        self.add('missing', 'missing.mp3')
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute('CREATE TABLE downloads(audio_url TEXT, file_path TEXT, status TEXT)')
            conn.execute('INSERT INTO downloads VALUES(?,?,?)', ('good', str(good), 'done'))
        stats = self.run_transcribe(FakeModel(['bad.mp3']))
        self.assertEqual((stats['done'], stats['failed'], stats['missing']), (1, 1, 1))
        # Succès du deuxième audio validé même si le premier échoue.
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            self.assertIsNone(conn.execute("SELECT transcription FROM audios WHERE audio_url='bad'").fetchone()[0])
            self.assertIsNotNone(conn.execute("SELECT transcription FROM audios WHERE audio_url='good'").fetchone()[0])
        self.assertEqual(self.run_transcribe(FakeModel(), limit=1)['done'], 1)

    def test_empty_transcript_completed_and_atomic_rollback(self):
        audio_id = self.add('empty', self.audio('empty.wav'))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            prepare_database(conn)
            save_transcription(conn, [audio_id], dict(text='', language='fr', segments=[]), 'turbo')
        self.assertEqual(self.run_transcribe(FakeModel())['done'], 0)
        second = self.add('two', self.audio('two.wav'))
        third = self.add('three', self.audio('three.wav'))
        with contextlib.closing(sqlite3.connect(self.db_path)) as conn, conn:
            conn.execute(f'''CREATE TRIGGER reject_third BEFORE UPDATE ON audios WHEN NEW.id={third}
                             BEGIN SELECT RAISE(ABORT, 'simulated disk error'); END''')
            conn.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                save_transcription(conn, [second, third], dict(text='Test'), 'turbo')
            self.assertIsNone(conn.execute('SELECT transcription FROM audios WHERE id=?', (second,)).fetchone()[0])

    def test_no_creation_on_wrong_database_and_cli_aliases(self):
        missing = Path(self.tmp.name) / 'typo.db'
        with self.assertRaises(FileNotFoundError):
            transcribe(self.root, db_path=missing, model=FakeModel())
        self.assertFalse(missing.exists())
        with self.assertRaises(ValueError):
            self.run_transcribe(FakeModel(), limit=0)
        for alias in ('uk', 'auto', 'unknown'):
            self.assertEqual(build_parser().parse_args(['-l', alias]).lang, Lang.uk)

    def test_interruption_keeps_completed_files(self):
        self.add('one', self.audio('one.mp3'))
        self.add('two', self.audio('two.mp3'))

        class InterruptedModel(FakeModel):
            def transcribe(self, path, **options):
                if Path(path).name == 'two.mp3':
                    raise KeyboardInterrupt()
                return super().transcribe(path, **options)

        with self.assertRaises(KeyboardInterrupt):
            self.run_transcribe(InterruptedModel())
        model = FakeModel()
        self.assertEqual(self.run_transcribe(model)['done'], 1)
        self.assertEqual(Path(model.calls[0][0]).name, 'two.mp3')

    def test_integration_with_project_database_manager(self):
        # Reproduire le schéma réel du ZIP, puis rouvrir après la migration.
        actual_db = Path(self.tmp.name) / 'actual.db'
        audio = self.audio('real_schema.mp3')
        with contextlib.closing(SQLManager(actual_db)) as db:
            db.create_audio('page', 'source', 'Titre', 'Description', 'Français', '', '')
            db.cur.execute('UPDATE audios SET file_name=?', (str(audio),))
            db.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            stats = transcribe(self.root, db_path=actual_db, model=FakeModel())
        self.assertEqual(stats['rows_saved'], 1)
        with contextlib.closing(SQLManager(actual_db)) as db:
            row = db.cur.execute('SELECT title,lang,transcription FROM audios').fetchone()
            self.assertEqual(row, ('Titre', 'Français', 'Bonjour éà.'))
            db.create_audio('new_page', 'new_source', 'Nouveau', '', 'Anglais', '', '')
            db.commit()
            self.assertIsNone(db.cur.execute("SELECT transcription FROM audios WHERE audio_url='new_source'").fetchone()[0])


if __name__ == '__main__':
    unittest.main()
