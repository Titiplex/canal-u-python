import contextlib
import csv
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

import download_audios
from modules import crawling, downloading, parsing
from modules.db_manager import SQLManager
from modules.download_state import DownloadState


class RecoveryTests(unittest.TestCase):
    def test_aac_adts_and_invalid_headers(self):
        # Deux trames ADTS, fréquence 44.1 kHz, longueur 11 octets chacune.
        frame = bytes.fromhex('fff15080017ffc') + b'\x00' * 4
        self.assertEqual(downloading.audio_extension(frame * 2), '.aac')
        self.assertIsNone(downloading.audio_extension(bytes.fromhex('fff13c80017ffc') + b'\x00' * 30))
        self.assertIsNone(downloading.audio_extension(frame + b'not an ADTS frame'))
        tag = b'ID3\x04\x00\x00\x00\x00\x00\x00'
        self.assertEqual(downloading.audio_extension(tag + frame * 2), '.aac')

    def test_image_with_mpeg_bytes_is_not_audio(self):
        for prefix in (b'\x89PNG\r\n\x1a\n', b'\xff\xd8\xff', b'GIF89a', b'RIFF1234WEBP'):
            self.assertIsNone(downloading.audio_extension(prefix + b'\xff\xfb\x90\x00' * 20))

    def test_download_aac_image_diagnostic_and_timeout_forwarding(self):
        frame = bytes.fromhex('fff15080017ffc') + b'\x00' * 4
        with tempfile.TemporaryDirectory() as tmp:
            job = dict(audio_url='https://example.org/podcast', title='Titre', languages=['fr'], pages=[])
            for body, mime in ((frame * 500, 'audio/aac'), (b'\x89PNG\r\n\x1a\n' + b'x' * 40, 'image/png')):
                response = requests.Response()
                response.status_code, response.url = 200, job['audio_url']
                response.headers.update({'Content-Type': mime, 'Content-Length': str(len(body))})
                response.raw = io.BytesIO(body)
                # Un dossier distinct évite de réutiliser le premier reçu.
                root = Path(tmp) / mime.split('/')[1]
                crawling.configure(0)
                with patch.object(downloading, '_request', return_value=response) as request:
                    if mime == 'image/png':
                        with self.assertRaisesRegex(downloading.InvalidAudio, 'Image PNG.*MIME=image/png'):
                            downloading.download(job, root, read_timeout=180)
                        self.assertFalse(list(root.rglob('*.aac')))
                    else:
                        result = downloading.download(job, root, read_timeout=180)
                        self.assertTrue(result['file'].endswith('.aac'))
                        self.assertEqual(downloading.existing(job, root), result)
                    self.assertEqual(request.call_args.kwargs['timeout'], (10, 180))

    def test_receipt_traversal(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'corpus'
            job = dict(audio_url='u', title='t', languages=[], pages=[])
            good = root / 'good.mp3'
            good.parent.mkdir()
            good.write_bytes(b'ID3xxxx')
            outside = Path(tmp) / 'outside.mp3'
            outside.write_bytes(b'ID3xxxx')
            _, _, receipt = downloading.paths(job, root)
            data = dict(audio_url='u', complete=True, file='good.mp3', size_bytes=7)
            downloading.atomic_json(receipt, data)
            self.assertEqual(downloading.existing(job, root), data)
            for unsafe in ('../outside.mp3', str(outside)):
                downloading.atomic_json(receipt, dict(data, file=unsafe))
                self.assertIsNone(downloading.existing(job, root))

    def test_error_export_and_reset_preserve_done_and_missing(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.closing(SQLManager(Path(tmp) / 'data.db')) as db:
            root = Path(tmp) / 'corpus'
            state = DownloadState(db, root)
            for url, error in [('aac', downloading.InvalidAudio('ancienne signature')),
                               ('gone', downloading.MissingAudio('HTTP 410')),
                               ('slow', requests.ReadTimeout('timeout'))]:
                db.create_audio('page-' + url, url, 'Titre', '', 'fr', '', '')
                state.fail(url, error)
            state.save('done', dict(file='ok.mp3', size_bytes=7, final_url='done'))
            path = root / '_state' / 'download_errors.csv'
            state.export_errors(path)
            with path.open(encoding='utf-8-sig', newline='') as stream:
                rows = {row['audio_url']: row for row in csv.DictReader(stream)}
            self.assertEqual(set(rows), {'aac', 'gone', 'slow'})
            self.assertEqual(rows['aac']['pages'], 'page-aac')
            state.reset_failed()
            statuses = dict(db.cur.execute('SELECT audio_url,status FROM downloads'))
            self.assertEqual(statuses, {'aac': 'pending', 'gone': 'missing', 'slow': 'pending', 'done': 'done'})

    def test_parse_other_audio_mime_but_not_video(self):
        html = '''<html><article class="node--view-mode-full"><h1>T</h1>
                  <audio><source src="/aac" type="audio/aac"></audio>
                  <video><source src="/video" type="video/mp4"></video></article></html>'''
        _, result = parsing.parse_page(html, 'https://example.org/page')
        self.assertEqual(result['audios'], ['https://example.org/aac'])

    def test_cli_read_timeout(self):
        parser = download_audios.build_parser()
        self.assertEqual(parser.parse_args(['--read-timeout', '180']).read_timeout, 180)


if __name__ == '__main__':
    unittest.main()
