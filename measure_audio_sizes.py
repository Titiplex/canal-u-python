"""Étape indépendante : mesurer les audios déjà découverts, sans revisiter leurs pages."""
import argparse
import contextlib
import logging
import time
from datetime import datetime
from pathlib import Path

from main import _stage
from modules import audio_sizes, crawling
from modules.db_manager import SQLManager
from modules.process_lock import DatabaseRunLock


def measure(db_path=None, workers=4, interval=2.0, summary_only=False):
    if workers < 1 or not 0 <= interval < float('inf'):
        raise ValueError('Workers >= 1 et intervalle fini >= 0 requis')
    path = Path(db_path) if db_path else Path(__file__).resolve().parent / '.cache/data.db'
    with DatabaseRunLock(path), contextlib.closing(SQLManager(path)) as db:
        crawling.configure(interval)
        try:
            if summary_only:
                audio_sizes.report(db)
                return None
            if db.pause_until() > time.time():
                print('Pause globale jusqu’à', datetime.fromtimestamp(db.pause_until()))
                audio_sizes.report(db)
                return db.pause_until()

            def save(url, result):
                db.save_audio_size(url, result)
                db.commit()
                return []

            def defer(url, error):
                db.rollback()
                db.defer_audio_size(url, error)
                logging.error('Taille à reprendre : %s : %s', url, error)

            print('Mesure des tailles audio...')
            _stage(db.pending_audio_sizes(), workers, audio_sizes.probe, save, defer)
            audio_sizes.report(db)
            if crawling.stopped() and db.pending_audio_sizes():
                return max(db.pause_until(), db.next_audio_size_retry() or 0)
            return db.next_audio_size_retry()
        finally:
            crawling.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=None)
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--interval', type=float, default=2.0)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--summary', action='store_true', help='Afficher le bilan enregistré, sans HTTP')
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    try:
        while True:
            deadline = measure(args.db, args.workers, args.interval, args.summary)
            if deadline is None or not args.watch:
                break
            print('Prochaine reprise :', datetime.fromtimestamp(deadline))
            while time.time() < deadline:
                time.sleep(max(0, min(30, deadline - time.time())))
    except KeyboardInterrupt:
        print('\nArrêt demandé ; les mesures validées sont conservées.')
