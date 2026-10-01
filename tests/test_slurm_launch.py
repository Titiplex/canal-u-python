import contextlib
import io
import os
import sqlite3
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import launch_transcriptions as launcher
import transcribe_slurm as pipeline


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.root = self.base / 'audio'
        self.root.mkdir()
        self.db = self.base / 'data.db'
        audio = self.root / 'example.mp3'
        audio.write_bytes(b'fake audio for planning')
        with contextlib.closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute('CREATE TABLE audios(id INTEGER PRIMARY KEY,url TEXT,audio_url TEXT,file_name TEXT)')
            conn.execute('INSERT INTO audios VALUES(1,?,?,?)', ('page', 'audio', str(audio)))
        self.plan = self.base / 'plan'
        self.batch = self.base / 'transcribe_array.sh'
        self.batch.write_text('#!/bin/bash\n#SBATCH --ntasks=1\n')
        self.stack = contextlib.ExitStack()
        self.stack.enter_context(contextlib.redirect_stdout(io.StringIO()))
        self.stack.enter_context(patch.object(launcher, 'PROJECT', self.base))
        self.which = self.stack.enter_context(patch.object(launcher.shutil, 'which', return_value='/usr/bin/sbatch'))
        self.submit = self.stack.enter_context(patch.object(launcher.subprocess, 'run'))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def launch(self, **kwargs):
        return launcher.launch(self.plan, db_path=self.db, dir_path=self.root, **kwargs)

    def test_prepare_then_submit_absolute_plan_with_requested_array(self):
        meta = self.launch(num_shards=3, max_parallel=2)
        self.assertEqual(meta['total'], 1)
        self.assertTrue((self.plan / 'plan.json').is_file())
        self.assertTrue((self.base / 'logs').is_dir())
        self.submit.assert_called_once_with(
            ['sbatch', '--chdir=' + str(self.base), '--array=0-2%2', str(self.batch), str(self.plan)], check=True)
        jobs = [job for index in range(3) for job in pipeline._read_shard(self.plan, meta, index)]
        self.assertEqual(len(jobs), 1)

    def test_existing_plan_reused_and_shard_count_inferred(self):
        self.launch(num_shards=3)
        original = (self.plan / 'plan.json').read_bytes()
        self.submit.reset_mock()
        with patch.object(launcher, 'prepare', side_effect=AssertionError('Plan rebuilt')):
            self.launch(max_parallel=8)
        self.assertEqual(original, (self.plan / 'plan.json').read_bytes())
        self.assertIn('--array=0-2%8', self.submit.call_args.args[0])

    def test_incomplete_plan_directory_refused(self):
        self.plan.mkdir()
        sentinel = self.plan / 'keep.txt'
        sentinel.write_text('Keep recoverable results')
        with self.assertRaisesRegex(ValueError, 'sans plan.json'):
            self.launch()
        self.assertTrue(sentinel.is_file())
        self.submit.assert_not_called()

    def test_mismatched_existing_plan_refused(self):
        self.launch(num_shards=3)
        self.submit.reset_mock()
        with self.assertRaisesRegex(ValueError, 'num_shards'):
            self.launch(num_shards=4)
        self.submit.assert_not_called()

    def test_missing_shard_refused_before_submission(self):
        self.launch(num_shards=3)
        self.submit.reset_mock()
        (self.plan / 'shards' / '00001.json').unlink()
        with self.assertRaises(FileNotFoundError):
            self.launch()
        self.submit.assert_not_called()

    def test_no_sbatch_does_not_prepare_or_submit(self):
        self.which.return_value = None
        with self.assertRaisesRegex(RuntimeError, 'sbatch introuvable'):
            self.launch()
        self.assertFalse(self.plan.exists())
        self.submit.assert_not_called()

    def test_preparation_failure_does_not_submit(self):
        with patch.object(launcher, 'prepare', side_effect=sqlite3.OperationalError('locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.launch()
        self.submit.assert_not_called()

    def test_multiple_tasks_refused_before_preparation(self):
        self.batch.write_text('#!/bin/bash\n#SBATCH --ntasks=4\n')
        with self.assertRaisesRegex(ValueError, '--ntasks=1'):
            self.launch()
        self.assertFalse(self.plan.exists())
        self.submit.assert_not_called()

    def test_sbatch_failure_keeps_plan_for_retry(self):
        self.submit.side_effect = subprocess.CalledProcessError(1, ['sbatch'])
        with self.assertRaises(subprocess.CalledProcessError):
            self.launch(num_shards=3)
        self.assertTrue((self.plan / 'plan.json').is_file())
        self.submit.side_effect = None
        with patch.object(launcher, 'prepare', side_effect=AssertionError('Plan rebuilt')):
            self.launch()


class BatchTests(unittest.TestCase):
    def test_spooled_script_uses_slurm_working_directory(self):
        # Slurm executes a copied script, whose dirname is a spool directory.
        script = Path(launcher.__file__).with_name('transcribe_array.sh').read_text()
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            project = base / 'project'
            (project / '.venv-whisper' / 'bin').mkdir(parents=True)
            (project / '.venv-whisper' / 'bin' / 'activate').write_text('true\n')
            spool = base / 'spool'
            spool.mkdir()
            batch = spool / 'slurm_script'
            batch.write_text(script)
            env = dict(os.environ, SLURM_JOB_ID='123')
            result = subprocess.run(['bash', str(batch)], cwd=project, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn(str(project / 'transcription_batches' / 'plan.json'), result.stderr)
            self.assertIn('Plan introuvable', result.stderr)


if __name__ == '__main__':
    unittest.main()
