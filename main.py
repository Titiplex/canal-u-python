import logging
import time
from collections import deque
from datetime import datetime

from modules import crawling, parsing
from modules import db_manager as dbm
from modules.visual import loadingbar as lb


def main(max_search_pages=None, db_path=None):
    db = dbm.SQLManager(db_path)
    print(f'Base SQLite : {db.path.resolve()}')

    def defer(kind, url, error):
        db.rollback()
        blocked = isinstance(error, crawling.RetryLater) and error.blocked
        deadline = db.defer(kind, url, error, getattr(error, 'delay', 60), blocked)
        message = datetime.fromtimestamp(deadline).strftime('%H:%M:%S') if deadline else 'suspendue après 5 échecs'
        logging.error('%s : %s ; reprise %s', url, error, message)
        return blocked

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
                if defer('count', count_url, error):
                    return db.next_retry()
        if max_search_pages is not None:
            count = min(count, max(0, max_search_pages))
        print('Recherche des pages...')
        bar = lb.LoadingBar(count)
        bar.print()
        for i in range(count):
            url = crawling.SEARCH_URL + str(i)
            if not db.is_visited(url) and db.retry_ready('search', url):
                try:
                    res = parsing.get_search_items(crawling.crawl(url), url)
                    if not res:
                        raise ValueError('Page de recherche sans résultat parsable')
                    db.save_to_queue(res)
                    db.add_visited(url)
                    db.clear_retry('search', url)
                    db.commit()
                except Exception as error:
                    if defer('search', url, error):
                        return db.next_retry()
            bar.increment()
            bar.print()
        print('\nTraitement des pages individuelles...')
        queue = deque(row for row in db.get_queue() if db.retry_ready('page', row[0]))
        bar = lb.LoadingBar(len(queue))
        bar.print()
        while queue:
            url, type_ = queue.popleft()
            try:
                html = crawling.crawl(url)
                found, metadata = parsing.parse_collection(html, url)
                for audio_url in metadata['audios']:
                    db.create_audio(url, audio_url, metadata['titre'], metadata['desc'],
                                    '; '.join(metadata['langues']), metadata['citation'],
                                    metadata['cdt'], metadata['lieu'], metadata['doi'])
                db.add_visited(url)
                added = db.save_to_queue(metadata['pages'])
                db.remove_from_queue(url)
                db.clear_retry('page', url)
                db.commit()
                queue.extend(added)
                bar.total += len(added)
            except Exception as error:
                if defer('page', url, error):
                    return db.next_retry()
            bar.increment()
            bar.print()
        print(f'\nPages restant en file : {db.get_queue_size()}. Reprises suspendues : {db.suspended_count()}')
        return db.next_retry()
    finally:
        db.close()
        try:
            crawling.close()
        except Exception:
            logging.exception('Erreur à la fermeture du navigateur')


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--max-search-pages', type=int, default=None)
    parser.add_argument('--db', default=None)
    parser.add_argument('--watch', action='store_true', help='Attendre les reprises programmées puis relancer')
    parser.add_argument('--no-browser', action='store_true', help='Requests uniquement avec reprise différée')
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    crawling.USE_BROWSER_FALLBACK = not args.no_browser
    while True:
        deadline = main(args.max_search_pages, args.db)
        if deadline is None:
            break
        print('Prochaine reprise possible :', datetime.fromtimestamp(deadline).strftime('%Y-%m-%d %H:%M:%S'))
        if not args.watch:
            break
        while time.time() < deadline:
            time.sleep(max(0, min(30, deadline - time.time())))
