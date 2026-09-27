import contextlib
import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import main
from modules import crawling
from modules.visual.loadingbar import LoadingBar


class TimingTests(unittest.TestCase):
    def test_elapsed_remaining_finish_and_midnight(self):
        with patch('modules.visual.loadingbar.time.monotonic', side_effect=[0, 60]), patch(
                'modules.visual.loadingbar.datetime') as clock, contextlib.redirect_stdout(io.StringIO()) as output:
            clock.now.return_value = datetime(2026, 9, 26, 23, 59, 30)
            bar = LoadingBar(4)
            bar.increment()
            bar.print()
        value = output.getvalue()
        self.assertIn('Écoulé : 0j 00:01:00', value)
        self.assertIn('Restant : 0j 00:03:00', value)
        self.assertIn('Fin estimée : 27/09 00:02:30', value)

    def test_no_eta_before_progress_and_none_during_pause(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            bar = LoadingBar(2)
            bar.print()
            bar.increment()
            bar.print(force=True, paused=True)
        self.assertIn('Restant : --:--:--', output.getvalue())
        self.assertIn('Restant : pause | Fin estimée : --', output.getvalue())

    def test_growing_queue_updates_estimate(self):
        with patch('modules.visual.loadingbar.time.monotonic', side_effect=[0, 10, 20]), contextlib.redirect_stdout(
                io.StringIO()) as output:
            bar = LoadingBar(2)
            bar.increment()
            bar.print()
            bar.total = 5
            bar.print()
        self.assertIn('Restant : 0j 00:01:20', output.getvalue())

    def test_separate_stage_intervals(self):
        intervals = []
        with tempfile.TemporaryDirectory() as tmp, patch.object(crawling, 'get_results_count',
                                                                return_value=0), patch.object(main, '_stage',
                                                                                              side_effect=lambda
                                                                                                      *a: intervals.append(
                                                                                                      crawling._gate.interval)), contextlib.redirect_stdout(
                io.StringIO()):
            main.main(db_path=Path(tmp) / 'data.db', interval=2, search_interval=0.5, page_interval=1.5)
        self.assertEqual(intervals, [0.5, 1.5])

    def test_interval_change_preserves_last_request_and_stop(self):
        crawling.configure(2)
        crawling._gate.last = 123
        crawling.stop()
        crawling.set_interval(1)
        self.assertEqual(crawling._gate.last, 123)
        self.assertTrue(crawling.stopped())
        crawling.configure()


if __name__ == '__main__':
    unittest.main()
