import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from queue import Empty

from modules import crawling, parsing
from modules import db_manager as dbm
from modules.process_lock import DatabaseRunLock
from modules.visual import loadingbar as lb
from modules.workers import WorkerPool


def _search(url):
    items = parsing.get_search_items(crawling.crawl(url), url)
    if not items:
        raise ValueError('Page de recherche sans résultat parsable')
    return items


def _page(url):
    return parsing.parse_collection(crawling.crawl(url), url)[1]


def _stage(urls, workers, fetch, save, defer):
    """Seul le coordinateur appelle save/defer ; au plus workers tâches en vol."""
    pending = deque(dict.fromkeys(urls))
    scheduled = set(pending)
    bar = lb.LoadingBar(len(pending))
    bar.print()
    active = 0
    with WorkerPool(workers, fetch) as pool:
        while pending or active:
            while pending and active < workers and not crawling.stopped():
                pool.submit(pending.popleft())
                active += 1
            if not active:
                break
            try:
                url, value, error = pool.results.get(timeout=0.2)
            except Empty:
                continue
            active -= 1
            if isinstance(error, crawling.CrawlCancelled):
                # Une annulation n'est pas un échec HTTP : aucun compteur de retry.
                continue
            if error is not None:
                if not isinstance(error, Exception):
                    raise error
                defer(url, error)
            else:
                try:
                    added = save(url, value)
                except Exception as error:
                    defer(url, error)
                else:
                    for child in added:
                        if child not in scheduled:
                            scheduled.add(child)
                            pending.append(child)
                            bar.total += 1
            bar.increment()
            bar.print()
    # Même après un blocage, tous les résultats déjà en vol ont été drainés.
    print()


def main(max_search_pages=None, db_path=None, search_workers=4, page_workers=4, interval=None):
    if search_workers < 1 or page_workers < 1:
        raise ValueError('Le nombre de workers doit être >= 1')
    if interval is not None and (not 0 <= interval < float('inf')):
        raise ValueError('Intervalle invalide')
    path = Path(db_path) if db_path else Path(__file__).resolve().parent / '.cache' / 'data.db'
    with DatabaseRunLock(path):
        crawling.configure(interval)
        return _main(max_search_pages, path, search_workers, page_workers)


def _main(max_search_pages, path, search_workers, page_workers):
    db = dbm.SQLManager(path)
    print(f'Base SQLite : {db.path.resolve()}')
    print(f'Workers recherche : {search_workers} ; pages : {page_workers}')

    def defer(kind, url, error):
        db.rollback()
        blocked = isinstance(error, crawling.RetryLater) and error.blocked
        if blocked:
            crawling.stop()
        deadline = db.defer(kind, url, error, getattr(error, 'delay', 60), blocked)
        message = datetime.fromtimestamp(deadline).strftime('%H:%M:%S') if deadline else 'suspendue après 5 échecs'
        logging.error('%s : %s ; reprise %s', url, error, message)

    def save_search(url, items):
        db.save_to_queue(items)
        db.add_visited(url)
        db.clear_retry('search', url)
        db.commit()
        return []

    def save_page(url, metadata):
        for audio_url in metadata['audios']:
            db.create_audio(url, audio_url, metadata['titre'], metadata['desc'],
                            '; '.join(metadata['langues']), metadata['citation'],
                            metadata['cdt'], metadata['lieu'], metadata['doi'])
        db.add_visited(url)
        added = db.save_to_queue(metadata['pages'])
        db.remove_from_queue(url)
        db.clear_retry('page', url)
        ready = [child for child, _ in added if db.retry_ready('page', child)]
        db.commit()
        return ready

    try:
        if db.pause_until() > time.time():
            print('Pause globale jusqu’à', datetime.fromtimestamp(db.pause_until()).strftime('%H:%M:%S'))
            return db.next_retry()
        count_url = crawling.SEARCH_URL + '0'
        count = 0
        if db.retry_ready('count', count_url):
            try:
                count = crawling.get_results_count()
                db.clear_retry('count', count_url)
                db.commit()
            except Exception as error:
                defer('count', count_url, error)
                if crawling.stopped():
                    return db.next_retry()
        crawling.close()  # Le client du compteur n'est pas partagé avec les workers.
        if max_search_pages is not None:
            count = min(count, max(0, max_search_pages))
        urls = (crawling.SEARCH_URL + str(i) for i in range(count))
        print('Recherche des pages...')
        _stage((url for url in urls if not db.is_visited(url) and db.retry_ready('search', url)),
               search_workers, _search, save_search, lambda u, e: defer('search', u, e))
        if crawling.stopped():
            return db.next_retry()
        print('Traitement des pages individuelles...')
        _stage((url for url, _ in db.get_queue() if db.retry_ready('page', url)),
               page_workers, _page, save_page, lambda u, e: defer('page', u, e))
        print(f'Pages restant en file : {db.get_queue_size()}. Reprises suspendues : {db.suspended_count()}')
        return db.next_retry()
    finally:
        db.close()
        crawling.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--max-search-pages', type=int, default=None)
    parser.add_argument('--db', default=None)
    parser.add_argument('--workers', type=int, default=4, help='Workers par étape (défaut : 4)')
    parser.add_argument('--search-workers', type=int, default=None)
    parser.add_argument('--page-workers', type=int, default=None)
    parser.add_argument('--interval', type=float, default=2.0, help='Intervalle GLOBAL entre départs HTTP')
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--trace-http', action='store_true')
    args = parser.parse_args()
    search_workers = args.search_workers if args.search_workers is not None else args.workers
    page_workers = args.page_workers if args.page_workers is not None else args.workers
    if min(search_workers, page_workers) < 1 or not 0 <= args.interval < float('inf'):
        parser.error('Workers >= 1 et intervalle fini >= 0 requis')
    logging.basicConfig(level=logging.ERROR)
    crawling.USE_BROWSER_FALLBACK = not args.no_browser
    crawling.TRACE_HTTP = args.trace_http
    try:
        while True:
            deadline = main(args.max_search_pages, args.db, search_workers, page_workers, args.interval)
            if deadline is None:
                break
            print('Prochaine reprise possible :', datetime.fromtimestamp(deadline).strftime('%Y-%m-%d %H:%M:%S'))
            if not args.watch:
                break
            while time.time() < deadline:
                time.sleep(max(0, min(30, deadline - time.time())))
    except KeyboardInterrupt:
        print('\nArrêt demandé ; les pages non validées restent à reprendre.')
