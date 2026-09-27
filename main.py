import hashlib
import logging
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from queue import Empty

from modules import audio_sizes, crawling, parsing
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
    html = crawling.crawl(url)
    try:
        return parsing.parse_collection(html, url)[1]
    except parsing.UnrecognizedPage as error:
        error.html = html
        raise


def _stage(urls, workers, fetch, save, defer, priority=None, unit='page'):
    """Seul le coordinateur appelle save/defer ; au plus workers tâches en vol."""
    pending = deque(dict.fromkeys(urls))
    scheduled = set(pending)
    bar = lb.LoadingBar(len(pending), unit=unit)
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
                bar.print(paused=crawling.stopped())
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
                            if priority and priority(child):
                                pending.appendleft(child)
                            else:
                                pending.append(child)
                            bar.total += 1
            bar.increment()
            bar.print()
    # Même après un blocage, tous les résultats déjà en vol ont été drainés.
    bar.print(force=True, paused=crawling.stopped())
    bar.elapsed()


def main(max_search_pages=None, db_path=None, search_workers=4, page_workers=4,
         interval=None, search_interval=None, page_interval=None, measure_sizes=True):
    if search_workers < 1 or page_workers < 1:
        raise ValueError('Le nombre de workers doit être >= 1')
    for value in (interval, search_interval, page_interval):
        if value is not None and not 0 <= value < float('inf'):
            raise ValueError('Intervalle invalide')
    base_interval = crawling.MIN_INTERVAL if interval is None else interval
    search_interval = base_interval if search_interval is None else search_interval
    page_interval = base_interval if page_interval is None else page_interval
    path = Path(db_path) if db_path else Path(__file__).resolve().parent / '.cache' / 'data.db'
    with DatabaseRunLock(path):
        crawling.configure(search_interval)
        return _main(max_search_pages, path, search_workers, page_workers, page_interval, measure_sizes)


def _main(max_search_pages, path, search_workers, page_workers, page_interval, measure_sizes):
    db = dbm.SQLManager(path)
    print(f'Base SQLite : {db.path.resolve()}')
    print(f'Workers recherche : {search_workers} ; pages : {page_workers}')
    restored = db.upgrade_parser()
    if restored:
        print(f'Anciennes erreurs de parsing réactivées : {restored}')

    def next_retry():
        deadlines = [db.next_retry()]
        if measure_sizes:
            deadlines.append(db.next_audio_size_retry())
            if crawling.stopped() and db.pending_audio_sizes():
                deadlines.append(db.pause_until())
        return min((date for date in deadlines if date is not None), default=None)

    def defer(kind, url, error):
        db.rollback()
        if kind == 'page' and isinstance(error, (parsing.UnrecognizedPage, crawling.PermanentHTTPError)):
            status = 'a_verifier' if isinstance(error, parsing.UnrecognizedPage) else 'introuvable'
            detail = str(error)
            if getattr(error, 'html', None):
                folder = db.path.parent / 'pages_a_verifier'
                folder.mkdir(parents=True, exist_ok=True)
                snapshot = folder / (hashlib.sha256(url.encode()).hexdigest() + '.html')
                snapshot.write_text(error.html, encoding='utf-8')
                detail += f' ; HTML : {snapshot}'
            db.record_outcome(url, status, detail)
            db.add_visited(url)
            db.remove_from_queue(url)
            db.clear_retry(kind, url)
            db.commit()
            logging.warning('%s : %s ; pas de reprise automatique', url, detail)
            return
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
        status = metadata.get('status', 'exploitable' if metadata['audios'] or metadata['pages'] else 'sans_contenu')
        db.record_outcome(url, status,
                          f"{len(metadata['audios'])} audio(s), {len(metadata['pages'])} lien(s)")
        db.remove_from_queue(url)
        db.clear_retry('page', url)
        ready = [child for child, _ in added if db.retry_ready('page', child)]
        db.commit()
        return ready

    def fetch_task(task):
        kind, url = task
        return _page(url) if kind == 'page' else audio_sizes.probe(url)

    def save_task(task, value):
        kind, url = task
        if kind == 'size':
            db.save_audio_size(url, value)
            db.commit()
            return []
        children = save_page(url, value)
        # Métadonnées déjà validées : un échec de mesure ne les annule pas.
        sizes = [('size', audio) for audio in value['audios'] if db.audio_size_ready(audio)]
        return [('page', child) for child in children] + sizes

    def defer_task(task, error):
        kind, url = task
        if kind == 'page':
            defer(kind, url, error)
        else:
            db.rollback()
            if isinstance(error, crawling.RetryLater) and error.blocked:
                crawling.stop()
            db.defer_audio_size(url, error)
            logging.error('Taille à reprendre : %s : %s', url, error)

    try:
        if db.pause_until() > time.time():
            print('Pause globale jusqu’à', datetime.fromtimestamp(db.pause_until()).strftime('%H:%M:%S'))
            return max(db.pause_until(), next_retry() or 0)
        count_url = crawling.SEARCH_URL + '0'
        count = 0
        if max_search_pages != 0 and db.retry_ready('count', count_url):
            try:
                count = crawling.get_results_count()
                db.clear_retry('count', count_url)
                db.commit()
            except Exception as error:
                defer('count', count_url, error)
                if crawling.stopped():
                    return next_retry()
        crawling.close()  # Le client du compteur n'est pas partagé avec les workers.
        if max_search_pages is not None:
            count = min(count, max(0, max_search_pages))
        urls = (crawling.SEARCH_URL + str(i) for i in range(count))
        print('Recherche des pages...')
        _stage((url for url in urls if not db.is_visited(url) and db.retry_ready('search', url)),
               search_workers, _search, save_search, lambda u, e: defer('search', u, e))
        if crawling.stopped():
            return next_retry()
        crawling.set_interval(page_interval)
        if measure_sizes:
            print('Traitement des pages individuelles et tailles audio...')
            tasks = [('page', url) for url, _ in db.get_queue() if db.retry_ready('page', url)]
            tasks += [('size', url) for url in db.pending_audio_sizes()]
            _stage(tasks, page_workers, fetch_task, save_task, defer_task,
                   priority=lambda task: task[0] == 'size', unit='tâche')
        else:
            print('Traitement des pages individuelles...')
            _stage((url for url, _ in db.get_queue() if db.retry_ready('page', url)),
                   page_workers, _page, save_page, lambda u, e: defer('page', u, e))
        print(f'Pages restant en file : {db.get_queue_size()}. Reprises suspendues : {db.suspended_count()}')
        print('Résultats enregistrés :', db.outcome_counts())
        return next_retry()
    finally:
        try:
            if measure_sizes:
                audio_sizes.report(db)
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
    parser.add_argument('--search-interval', type=float, default=None)
    parser.add_argument('--page-interval', type=float, default=None)
    parser.add_argument('--watch', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--no-audio-sizes', action='store_true', help='Désactiver la mesure des tailles pendant le crawl')
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
            deadline = main(args.max_search_pages, args.db, search_workers, page_workers,
                            args.interval, args.search_interval, args.page_interval,
                            measure_sizes=not args.no_audio_sizes)
            if deadline is None:
                break
            print('Prochaine reprise possible :', datetime.fromtimestamp(deadline).strftime('%Y-%m-%d %H:%M:%S'))
            if not args.watch:
                break
            while time.time() < deadline:
                time.sleep(max(0, min(30, deadline - time.time())))
    except KeyboardInterrupt:
        print('\nArrêt demandé ; les pages non validées restent à reprendre.')
