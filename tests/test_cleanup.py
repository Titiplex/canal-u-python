import contextlib
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import clean_audio_corpus as cleanup


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg/FFprobe requis')
class CleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.assets = tempfile.TemporaryDirectory()
        cls.asset_root = Path(cls.assets.name)
        for codec, extension, extra in [('aac', 'aac', ['-f', 'adts']), ('libmp3lame', 'mp3', [])]:
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
                            '-i', 'sine=frequency=800:duration=0.3', '-c:a', codec,
                            '-threads', '1', *extra, str(cls.asset_root / ('audio.' + extension))], check=True)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', 'color=black:size=16x16', '-frames:v', '1', '-threads', '1',
                        str(cls.asset_root / 'image.png')], check=True)
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-i', str(cls.asset_root / 'audio.mp3'),
                        '-i', str(cls.asset_root / 'image.png'), '-map', '0:a', '-map', '1:v', '-c', 'copy',
                        '-id3v2_version', '3', '-disposition:v', 'attached_pic',
                        str(cls.asset_root / 'cover.mp3')], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.assets.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'corpus'
        self.root.mkdir()
        self.db = self.base / 'data.db'
        self.conn = sqlite3.connect(str(self.db))
        self.conn.executescript('''CREATE TABLE audios (
          audio_url TEXT,file_name TEXT,url TEXT,title TEXT,lang TEXT);
          CREATE TABLE audio_sizes (
          audio_url TEXT PRIMARY KEY,size_bytes INTEGER,final_url TEXT,status TEXT,
          method TEXT,detail TEXT,checked_at REAL,attempts INTEGER,next_attempt REAL);''')
        cleanup.ensure_write_schema(self.conn)

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def args(self, *extra):
        return cleanup.build_parser().parse_args(['--db', str(self.db), '--root', str(self.root),
                         '--report', str(self.base / 'report.csv'), '--workers', '2', *extra])

    def call(self, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cleanup.run(self.args(*extra))

    def seed(self, name, asset, url=None, filename=None):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(str(self.asset_root / asset), str(path))
        url = url or 'https://example.org/' + name
        key = hashlib.sha256(url.encode()).hexdigest()
        receipt = self.root / '_state' / (key + '.json')
        cleanup.json_write(receipt, dict(audio_url=url, file=path.relative_to(self.root).as_posix(),
                           complete=True, size_bytes=path.stat().st_size, final_url=url,
                           title='Titre', pages=['page'], languages=['fr']))
        self.conn.execute('INSERT INTO audios VALUES (?,?,?,?,?)',
                          (url, filename if filename is not None else str(path), 'page', 'Titre', 'fr'))
        self.conn.execute('INSERT INTO audio_sizes VALUES (?,?,?,?,?,?,?,?,?)',
                          (url, path.stat().st_size, url, 'known', 'DOWNLOAD', '', 1, 0, None))
        cleanup.set_download(self.conn, self.root, url, 'done', str(path), path.stat().st_size, '')
        self.conn.commit()
        return path, url, receipt

    def backup(self):
        return next(self.base.glob('corpus_cleanup_*'))

    def test_dry_run_leaves_corpus_database_and_receipts(self):
        path, url, receipt = self.seed('fake.mp3', 'image.png')
        before = (path.read_bytes(), receipt.read_bytes(), list(self.conn.iterdump()))
        self.assertEqual(self.call(), 0)
        self.assertEqual(before, (path.read_bytes(), receipt.read_bytes(), list(self.conn.iterdump())))
        self.assertFalse(list(self.base.glob('corpus_cleanup_*')))

    def test_remove_image_clears_references_sizes_receipt_and_partials(self):
        path, url, receipt = self.seed('fr/image.mp3', 'image.png', filename='./fr/image.mp3')
        original = path.read_bytes()
        partial = receipt.with_name(receipt.stem + '.partial.json')
        partial.write_text('{}')
        path.with_suffix('.part').write_bytes(b'stale')
        self.assertEqual(self.call('--apply'), 0)
        self.assertFalse(path.exists())
        self.assertFalse(receipt.exists())
        self.assertFalse(partial.exists())
        self.assertFalse(path.with_suffix('.part').exists())
        self.assertEqual(self.conn.execute('SELECT status,file_path,size_bytes,next_attempt FROM downloads').fetchone(),
                         ('invalid', None, None, None))
        self.assertEqual(self.conn.execute('SELECT file_name,url,title FROM audios').fetchone(), ('', 'page', 'Titre'))
        self.assertEqual(self.conn.execute('SELECT status,size_bytes FROM audio_sizes').fetchone(), ('invalid', None))
        self.assertEqual((self.backup() / 'originals/fr/image.mp3').read_bytes(), original)
        self.assertTrue((self.backup() / 'database.sqlite').is_file())

    def test_convert_misnamed_aac_in_place_and_keep_remote_size(self):
        path, url, receipt = self.seed('fake.mp3', 'audio.aac')
        original = path.read_bytes()
        remote_size = len(original)
        self.conn.execute('INSERT INTO audios VALUES (?,?,?,?,?)', (url, '', 'page2', 'Titre', 'en'))
        self.conn.commit()
        self.assertEqual(self.call('--apply'), 0)
        data = cleanup.probe(path, shutil.which('ffprobe'), 30)
        self.assertEqual(data['streams'][0]['codec_name'], 'mp3')
        updated = cleanup.json_read(receipt)
        self.assertEqual(updated['size_bytes'], path.stat().st_size)
        self.assertEqual(updated['local_cleanup']['action'], 'convert')
        self.assertEqual(self.conn.execute('SELECT size_bytes FROM downloads').fetchone()[0], path.stat().st_size)
        self.assertEqual(self.conn.execute('SELECT size_bytes FROM audio_sizes').fetchone()[0], remote_size)
        self.assertEqual([r[0] for r in self.conn.execute('SELECT file_name FROM audios')], [str(path), str(path)])
        self.assertEqual((self.backup() / 'originals/fake.mp3').read_bytes(), original)

    def test_convert_aac_extension_and_update_path(self):
        path, url, receipt = self.seed('real.aac', 'audio.aac')
        self.assertEqual(self.call('--apply'), 0)
        self.assertFalse(path.exists())
        target = path.with_suffix('.mp3')
        self.assertTrue(target.exists())
        self.assertEqual(cleanup.json_read(receipt)['file'], 'real.mp3')
        self.assertEqual(self.conn.execute('SELECT file_name FROM audios').fetchone()[0], str(target))

    def test_rename_without_reencoding(self):
        path, url, receipt = self.seed('fake.mp3', 'audio.aac')
        original = path.read_bytes()
        self.assertEqual(self.call('--apply', '--aac-action', 'rename'), 0)
        target = path.with_suffix('.aac')
        self.assertFalse(path.exists())
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(cleanup.json_read(receipt)['file'], 'fake.aac')

    def test_mp3_cover_is_not_removed(self):
        path, url, receipt = self.seed('cover.mp3', 'cover.mp3')
        original = path.read_bytes()
        self.assertEqual(self.call('--apply'), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(self.conn.execute('SELECT status FROM downloads').fetchone()[0], 'done')

    def test_unknown_html_is_kept(self):
        path, url, receipt = self.seed('unknown.mp3', 'audio.mp3')
        path.write_bytes(b'<html>Server error</html>')
        self.assertEqual(self.call('--apply'), 0)
        self.assertEqual(path.read_bytes(), b'<html>Server error</html>')
        self.assertTrue(receipt.exists())

    def test_conversion_failure_does_not_modify_original_or_db(self):
        path, url, receipt = self.seed('fake.mp3', 'audio.aac')
        original, old_receipt = path.read_bytes(), receipt.read_bytes()
        with patch.object(cleanup, 'converted_temp', side_effect=RuntimeError('encoder failed')):
            self.assertEqual(self.call('--apply'), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(receipt.read_bytes(), old_receipt)
        self.assertEqual(self.conn.execute('SELECT status FROM downloads').fetchone()[0], 'done')

    def test_sql_failure_restores_removed_image_and_receipt(self):
        path, url, receipt = self.seed('fake.mp3', 'image.png')
        original, old_receipt = path.read_bytes(), receipt.read_bytes()
        with patch.object(cleanup, 'set_download', side_effect=sqlite3.OperationalError('write failed')):
            self.assertEqual(self.call('--apply'), 1)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(receipt.read_bytes(), old_receipt)
        self.assertEqual(self.conn.execute('SELECT status FROM downloads').fetchone()[0], 'done')
        self.assertEqual(cleanup.json_read(next((self.backup() / 'jobs').glob('*.json')))['status'], 'rolled_back')

    def test_crash_recovery_restores_uncommitted_operation(self):
        path, url, receipt = self.seed('fake.mp3', 'image.png')
        original, old_receipt = path.read_bytes(), receipt.read_bytes()
        backup = self.base / 'recovery'
        backup.mkdir()
        cleanup.json_write(backup / 'run.json', dict(run_id='run', root=str(self.root), db=str(self.db)))
        cleanup.backup_item(path, None, {url}, self.root, backup, 'item')
        path.unlink()
        receipt.unlink()
        cleanup.json_write(self.root / '_state/cleanup_pending.json', dict(backup=str(backup)))
        self.assertEqual(self.call('--recover', str(backup)), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(receipt.read_bytes(), old_receipt)
        self.assertFalse((self.root / '_state/cleanup_pending.json').exists())

    def test_crash_recovery_preserves_committed_conversion(self):
        path, url, receipt = self.seed('fake.mp3', 'audio.aac')
        self.assertEqual(self.call('--apply'), 0)
        converted = path.read_bytes()
        backup = self.backup()
        journal_path = next((backup / 'jobs').glob('*.json'))
        journal = cleanup.json_read(journal_path)
        journal['status'] = 'prepared'
        cleanup.json_write(journal_path, journal)
        self.assertEqual(self.call('--recover', str(backup)), 0)
        self.assertEqual(path.read_bytes(), converted)
        self.assertEqual(cleanup.json_read(journal_path)['status'], 'done')

    def test_collision_is_not_overwritten(self):
        path, url, receipt = self.seed('real.aac', 'audio.aac')
        target = path.with_suffix('.mp3')
        target.write_bytes((self.asset_root / 'audio.mp3').read_bytes())
        target_before = target.read_bytes()
        self.assertEqual(self.call('--apply'), 1)
        self.assertTrue(path.exists())
        self.assertEqual(target.read_bytes(), target_before)

    def test_lock_refuses_active_downloader(self):
        self.seed('fake.mp3', 'image.png')
        with cleanup.RunLock(self.db), self.assertRaisesRegex(RuntimeError, 'déjà'):
            self.call('--apply')

    def test_hash_recovers_link_without_receipt_or_file_name(self):
        url = 'https://example.org/hash'
        name = 'fr/title__' + hashlib.sha256(url.encode()).hexdigest()[:32] + '.mp3'
        path, url, receipt = self.seed(name, 'audio.aac', url=url, filename='')
        receipt.unlink()
        self.conn.execute('DELETE FROM downloads')
        self.conn.commit()
        self.assertEqual(self.call('--apply'), 0)
        self.assertTrue(cleanup.json_read(receipt)['complete'])
        self.assertEqual(self.conn.execute('SELECT file_name FROM audios').fetchone()[0], str(path))

    @unittest.skipIf(os.name == 'nt', 'Symlinks Unix')
    def test_external_symlink_is_not_modified(self):
        outside = self.base / 'outside.mp3'
        outside.write_bytes((self.asset_root / 'image.png').read_bytes())
        (self.root / 'linked.mp3').symlink_to(outside)
        self.assertEqual(self.call('--apply'), 0)
        self.assertTrue((self.root / 'linked.mp3').is_symlink())
        self.assertTrue(outside.exists())

    def test_other_root_copy_is_preserved(self):
        path, url, receipt = self.seed('fake.mp3', 'image.png')
        other_root = self.base / 'other'
        cleanup.set_download(self.conn, other_root, url, 'done', str(other_root / 'ok.mp3'), 123, '')
        self.conn.execute('INSERT INTO audios VALUES (?,?,?,?,?)', (url, str(other_root / 'ok.mp3'), 'other-page', 'Titre', 'fr'))
        self.conn.commit()
        self.assertEqual(self.call('--apply'), 0)
        self.assertEqual(self.conn.execute('SELECT status FROM downloads WHERE output_root=?', (str(other_root),)).fetchone()[0], 'done')
        self.assertEqual(self.conn.execute('SELECT file_name FROM audios WHERE url=?', ('other-page',)).fetchone()[0], str(other_root / 'ok.mp3'))
        self.assertEqual(self.conn.execute('SELECT status FROM audio_sizes').fetchone()[0], 'known')

    def test_ambiguous_url_has_no_changes(self):
        path, url, receipt = self.seed('fake.mp3', 'image.png')
        other = self.root / 'second.mp3'
        other.write_bytes(path.read_bytes())
        self.conn.execute('INSERT INTO audios VALUES (?,?,?,?,?)', (url, str(other), 'page2', 'Titre', 'fr'))
        self.conn.commit()
        self.assertEqual(self.call('--apply'), 1)
        self.assertTrue(path.exists())
        self.assertTrue(other.exists())

    def test_video_with_aac_is_kept(self):
        path = self.root / 'conference.mp4'
        subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-y', '-f', 'lavfi',
                        '-i', 'color=black:size=16x16:duration=0.3', '-f', 'lavfi',
                        '-i', 'sine=frequency=800:duration=0.3', '-c:v', 'mpeg4', '-c:a', 'aac',
                        '-threads', '1', str(path)], check=True)
        original = path.read_bytes()
        self.assertEqual(self.call('--apply'), 0)
        self.assertEqual(path.read_bytes(), original)
        self.assertFalse(path.with_suffix('.mp3').exists())

    def test_recovery_clears_interrupted_ffmpeg_temporary_file(self):
        backup = self.base / 'recovery'
        backup.mkdir()
        cleanup.json_write(backup / 'run.json', dict(run_id='run', root=str(self.root), db=str(self.db)))
        temp = self.root / '.cleanup-run-test.mp3'
        temp.write_bytes(b'incomplete output')
        other = self.root / '.cleanup-other-test.mp3'
        other.write_bytes(b'unrelated')
        self.assertEqual(self.call('--recover', str(backup)), 0)
        self.assertFalse(temp.exists())
        self.assertTrue(other.exists())

    def test_report_cannot_overwrite_db(self):
        with self.assertRaisesRegex(RuntimeError, 'rapport'):
            self.call('--report', str(self.db))


if __name__ == '__main__':
    unittest.main()
