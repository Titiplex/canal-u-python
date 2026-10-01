import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import transcribe_slurm as pipeline


class FakeModel:
    device = SimpleNamespace(type='cuda')

    def __init__(self, failures=(), interrupt=None):
        self.calls = []
        self.failures = set(failures)
        self.interrupt = interrupt

    def transcribe(self, path, **options):
        name = Path(path).name
        self.calls.append((name, options))
        if name == self.interrupt:
            raise KeyboardInterrupt()
        if name in self.failures:
            raise RuntimeError('Échec simulé')
        return dict(text=' Texte éà ' + name, language='fr',
                    segments=[dict(start=0.0, end=1.5, text=' Texte éà ' + name)])


class SlurmTranscriptionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / 'audio'
        self.root.mkdir()
        self.db = self.base / 'data.db'
        self.plan = self.base / 'plan'
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE audios(id INTEGER PRIMARY KEY,url TEXT,audio_url TEXT,file_name TEXT)')

    def tearDown(self):
        self.tmp.cleanup()

    def seed(self, index, size=10):
        path = self.root / f'{index}.mp3'
        path.write_bytes(bytes([index % 256]) * size)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            audio_id = conn.execute('INSERT INTO audios(url,audio_url,file_name) VALUES(?,?,?)',
                                   (f'page-{index}', f'audio-{index}', str(path))).lastrowid
        return path, audio_id

    def prepare(self, shards=3, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()):
            return pipeline.prepare(self.plan, self.db, self.root, shards, **kwargs)

    def jobs(self):
        _, meta = pipeline._read_plan(self.plan)
        return [pipeline._read_shard(self.plan, meta, index) for index in range(meta['num_shards'])]

    def run_all(self):
        stats = []
        with contextlib.redirect_stdout(io.StringIO()):
            for index in range(len(self.jobs())):
                stats.append(pipeline.run_shard(self.plan, index, model=FakeModel()))
        return stats

    def merge(self):
        with contextlib.redirect_stdout(io.StringIO()):
            return pipeline.merge(self.plan)

    def rows(self):
        with contextlib.closing(sqlite3.connect(self.db)) as conn:
            return conn.execute('SELECT id,transcription FROM audios ORDER BY id').fetchall()

    def test_static_balanced_cover_duplicates_and_existing_text(self):
        ids = []
        for index, size in enumerate([100, 90, 80, 70, 60, 50, 40, 30]):
            _, audio_id = self.seed(index, size)
            ids.append(audio_id)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("INSERT INTO audios(url,audio_url,file_name) VALUES('duplicate-page','audio-0','')")
            pipeline.prepare_database(conn)
            conn.execute('UPDATE audios SET transcription=? WHERE id=?', ('Déjà fait', ids[-1]))
        self.prepare(shards=3)
        jobs = [job for shard in self.jobs() for job in shard]
        self.assertEqual(len(jobs), 7)
        self.assertEqual(len({job['job_id'] for job in jobs}), 7)
        bound_ids = [row[0] for job in jobs for row in job['bindings']]
        self.assertEqual(len(bound_ids), 8)
        self.assertEqual(len(set(bound_ids)), 8)
        self.assertNotIn(ids[-1], bound_ids)
        loads = [sum(job['size_bytes'] for job in shard) for shard in self.jobs()]
        self.assertLessEqual(max(loads) - min(loads), 100)
        self.run_all()
        self.assertEqual(self.merge()['rows_saved'], 8)
        self.assertEqual(dict(self.rows())[ids[-1]], 'Déjà fait')
        self.assertEqual(self.merge()['already_imported'], 7)

    def test_concurrent_workers_never_connect_sqlite_and_are_disjoint(self):
        for index in range(11):
            self.seed(index, size=10 + index)
        self.prepare(shards=4)
        models = [FakeModel() for _ in range(4)]
        with contextlib.redirect_stdout(io.StringIO()), patch.object(
                pipeline.sqlite3, 'connect', side_effect=AssertionError('Worker touched SQLite')):
            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(pipeline.run_shard, self.plan, index, model=models[index])
                           for index in range(4)]
                for future in futures:
                    self.assertEqual(future.result()['remaining'], 0)
        calls = [name for model in models for name, _ in model.calls]
        self.assertEqual(len(calls), 11)
        self.assertEqual(len(set(calls)), 11)
        self.assertTrue(all(text is None for _, text in self.rows()))
        self.assertEqual(self.merge()['rows_saved'], 11)

    def test_failure_retry_and_partial_merge_do_not_shift_shards(self):
        for index in range(5):
            self.seed(index)
        self.prepare(shards=2, lang='uk')
        initial = self.jobs()
        failed_name = Path(initial[0][0]['file_path']).name
        model = FakeModel(failures=[failed_name])
        with contextlib.redirect_stdout(io.StringIO()):
            stats = pipeline.run_shard(self.plan, 0, model=model)
        self.assertEqual(stats['failed'], 1)
        self.assertIsNone(model.calls[0][1]['language'])
        self.assertTrue(model.calls[0][1]['fp16'])
        partial = self.merge()
        self.assertGreater(partial['missing'], 0)
        self.assertEqual(self.jobs(), initial)
        retry = FakeModel()
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.run_shard(self.plan, 0, model=retry)
            pipeline.run_shard(self.plan, 1, model=FakeModel())
        self.assertEqual([name for name, _ in retry.calls], [failed_name])
        final = self.merge()
        self.assertEqual(final['missing'], 0)
        self.assertEqual(sum(text is not None for _, text in self.rows()), 5)
        self.assertFalse(list((self.plan / 'results').rglob('*.error.json')))

    def test_interruption_preserves_completed_audio(self):
        for index in range(3):
            self.seed(index)
        self.prepare(shards=1)
        jobs = self.jobs()[0]
        stop_name = Path(jobs[1]['file_path']).name
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(KeyboardInterrupt):
            pipeline.run_shard(self.plan, 0, model=FakeModel(interrupt=stop_name))
        model = FakeModel()
        with contextlib.redirect_stdout(io.StringIO()):
            stats = pipeline.run_shard(self.plan, 0, model=model)
        self.assertEqual(stats['resumed'], 1)
        self.assertEqual(len(model.calls), 2)
        self.assertEqual(self.merge()['imported'], 3)

    def test_modified_source_or_reused_database_id_refused(self):
        path, audio_id = self.seed(0)
        _, second_id = self.seed(1)
        self.prepare(shards=1)
        self.run_all()
        path.write_bytes(b'changed audio')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE audios SET audio_url=? WHERE id=?', ('different-source', second_id))
        stats = self.merge()
        self.assertEqual(stats['rejected'], 2)
        self.assertTrue(all(text is None for _, text in self.rows()))

    def test_existing_manual_text_protected_and_overwrite_idempotent(self):
        _, audio_id = self.seed(0)
        self.prepare(shards=1)
        self.run_all()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('UPDATE audios SET transcription=? WHERE id=?', ('Texte corrigé', audio_id))
        self.assertEqual(self.merge()['rows_saved'], 0)
        self.assertEqual(self.rows()[0][1], 'Texte corrigé')
        self.plan = self.base / 'second-plan'
        self.prepare(shards=1, overwrite=True)
        self.run_all()
        self.assertEqual(self.merge()['rows_saved'], 1)
        first = self.rows()
        self.assertEqual(self.merge()['already_imported'], 1)
        self.assertEqual(self.rows(), first)

    def test_atomic_transaction_for_shared_source_and_import_marker(self):
        _, audio_id = self.seed(0)
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            duplicate_id = conn.execute("INSERT INTO audios(url,audio_url,file_name) VALUES('copy-page','audio-0','')").lastrowid
        self.prepare(shards=1)
        self.run_all()
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute(f'''CREATE TRIGGER reject BEFORE UPDATE ON audios WHEN NEW.id={duplicate_id}
                             BEGIN SELECT RAISE(ABORT, 'simulated write error'); END''')
        with self.assertRaises(sqlite3.IntegrityError):
            self.merge()
        self.assertEqual(self.rows(), [(audio_id, None), (duplicate_id, None)])
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute('SELECT COUNT(*) FROM transcription_array_imports').fetchone()[0], 0)
            conn.execute('DROP TRIGGER reject')
        self.assertEqual(self.merge()['rows_saved'], 2)

    def test_limits_empty_shards_validation_and_mismatched_result(self):
        for index in range(4):
            self.seed(index)
        self.prepare(shards=5, limit=2)
        self.assertEqual(sum(len(shard) for shard in self.jobs()), 2)
        self.run_all()
        result = next((self.plan / 'results').rglob('*.json'))
        payload = pipeline._read_json(result)
        payload['run_id'] = 'another-plan'
        pipeline._atomic_json(result, payload)
        self.assertEqual(self.merge()['rejected'], 1)
        with self.assertRaises(ValueError):
            pipeline.run_shard(self.plan, 5, model=FakeModel())
        with self.assertRaises(FileExistsError):
            self.prepare(shards=5)
        with self.assertRaises(ValueError):
            pipeline.prepare(self.base / 'bad', self.db, self.root, num_shards=0)

    def test_cpu_fp16_disabled_and_empty_transcript_is_complete(self):
        self.seed(0)
        self.prepare(shards=1)

        class SilentModel(FakeModel):
            device = SimpleNamespace(type='cpu')

            def transcribe(self, path, **options):
                self.calls.append((path, options))
                return dict(text='', segments=[], language='fr')

        model = SilentModel()
        with contextlib.redirect_stdout(io.StringIO()):
            pipeline.run_shard(self.plan, 0, model=model)
            stats = pipeline.run_shard(self.plan, 0, model=model)
        self.assertFalse(model.calls[0][1]['fp16'])
        self.assertEqual(stats['resumed'], 1)
        self.assertEqual(len(model.calls), 1)
        self.merge()
        self.assertEqual(self.rows()[0][1], '')

    def test_weight_loading_serialized_but_inference_parallel(self):
        for index in range(3):
            self.seed(index)
        self.prepare(shards=3)
        state = dict(active=0, maximum=0)
        mutex = threading.Lock()
        barrier = threading.Barrier(3)

        class ParallelModel(FakeModel):
            def transcribe(self, path, **options):
                barrier.wait(timeout=5)
                return super().transcribe(path, **options)

        def load_model(name, device):
            self.assertEqual((name, device), ('turbo', 'cuda'))
            with mutex:
                state['active'] += 1
                state['maximum'] = max(state['maximum'], state['active'])
            time.sleep(0.02)
            with mutex:
                state['active'] -= 1
            return ParallelModel()

        with contextlib.redirect_stdout(io.StringIO()), patch.dict(
                sys.modules, {'whisper': SimpleNamespace(load_model=load_model)}):
            with ThreadPoolExecutor(max_workers=3) as pool:
                futures = [pool.submit(pipeline.run_shard, self.plan, index) for index in range(3)]
                for future in futures:
                    self.assertEqual(future.result()['done'], 1)
        self.assertEqual(state['maximum'], 1)

    def test_cli_slurm_index_on_sparse_resubmission(self):
        for index in range(4):
            self.seed(index)
        self.prepare(shards=4)
        # SLURM_ARRAY_TASK_COUNT d'une reprise peut être 1, mais le plan reste à 4 lots.
        with contextlib.redirect_stdout(io.StringIO()), patch.dict(
                'os.environ', {'SLURM_ARRAY_TASK_ID': '3', 'SLURM_ARRAY_TASK_COUNT': '1'}), patch.object(
                sys, 'argv', ['transcribe_slurm.py', 'run', '--plan', str(self.plan)]), patch.dict(
                sys.modules, {'whisper': SimpleNamespace(load_model=lambda name, device: FakeModel())}):
            self.assertEqual(pipeline.main(), 0)
        self.assertTrue((self.plan / 'status/00003.json').exists())
        self.assertFalse((self.plan / 'status/00000.json').exists())


if __name__ == '__main__':
    unittest.main()
