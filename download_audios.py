"""Télécharge les URL audio de SQLite ; indépendant du lancement de main.py."""
import argparse
import contextlib
import logging
import sqlite3
import time
from datetime import datetime
from pathlib import Path

import requests

from main import _stage
from modules import crawling, downloading
from modules.db_manager import SQLManager
from modules.download_state import DownloadState
from modules.process_lock import DatabaseRunLock

PROJECT = Path(__file__).resolve().parent


def run(db_path=None, output=None, workers=4, interval=2.0, watch=False, limit=None, retry_failed=False,
        languages=None):
    if workers < 1 or not 0 <= interval < float('inf') or (limit is not None and limit < 1):
        raise ValueError('Workers et limite >= 1 ; intervalle fini >= 0')
    path = Path(db_path).resolve() if db_path else PROJECT / '.cache/data.db'
    if not path.is_file():
        raise FileNotFoundError(f'Base du crawl introuvable : {path}')
    root = Path(output).resolve() if output else PROJECT / 'corpus_audio'
    root.mkdir(parents=True, exist_ok=True)
    # Un coordinateur SQLite et un verrou du dossier même avec deux bases différentes.
    with DatabaseRunLock(path), DatabaseRunLock(root / '_state' / 'downloads'), contextlib.closing(
            SQLManager(path)) as db:
        state = DownloadState(db, root, languages=languages)
        if retry_failed:
            state.reset_failed()
        try:
            while True:
                crawling.configure(interval)
                jobs = state.jobs()
                if limit is not None:
                    jobs = dict(list(jobs.items())[:limit])
                if db.pause_until() > time.time():
                    deadline = db.pause_until()
                else:
                    print(f'Dossier : {root}\nTéléchargements prêts : {len(jobs)} ; workers : {workers}')

                    def save(url, result):
                        state.save(url, result)
                        return []

                    def defer(url, error):
                        db.rollback()
                        if isinstance(error, sqlite3.Error) or (
                                isinstance(error, OSError) and not isinstance(error, requests.RequestException)):
                            raise error  # Disque plein / problème DB : arrêter, pas insister.
                        status = state.fail(url, error)
                        logging.error('%s : %s : %s', status, url, error)

                    _stage(jobs, workers, lambda url: downloading.download(jobs[url], root),
                           save, defer, unit='fichier')
                    deadline = state.next_retry()
                    if crawling.stopped() and state.jobs():
                        deadline = max(db.pause_until(), deadline or 0)
                state.report()
                if deadline is None or not watch:
                    if deadline is not None:
                        print('Prochaine reprise :', datetime.fromtimestamp(deadline))
                    return deadline
                print('Attente jusqu’à', datetime.fromtimestamp(deadline))
                while time.time() < deadline:
                    time.sleep(max(0, min(30, deadline - time.time())))
        finally:
            crawling.close()


def build_parser():
    parser = argparse.ArgumentParser()
    parser.add_argument('--db', default=None)
    parser.add_argument('--output', default=None, help='Dossier dédié (défaut : corpus_audio dans le projet)')
    parser.add_argument('--workers', type=int, default=4)
    parser.add_argument('--interval', type=float, default=2.0,
                        help='Intervalle global entre départs HTTP ; débit des transferts non limité')
    parser.add_argument('--watch', action='store_true', help='Attendre et exécuter les reprises temporaires')
    parser.add_argument('--limit', type=int, default=None, help='Maximum de fichiers par passage')
    parser.add_argument('--retry-failed', action='store_true',
                        help='Réactiver erreurs suspendues/non audio (pas les 404/410)')
    parser.add_argument('--languages', '--lang', nargs='+', action='extend', default=None,
                        help='Valeurs exactes du champ lang ; "" pour vide/NULL. Sans option : toutes.')
    return parser


if __name__ == '__main__':
    args = build_parser().parse_args()
    logging.basicConfig(level=logging.ERROR)
    try:
        run(args.db, args.output, args.workers, args.interval, args.watch, args.limit, args.retry_failed,
            languages=args.languages)
    except KeyboardInterrupt:
        print('\nArrêt demandé ; téléchargements validés et fichiers partiels conservés.')
