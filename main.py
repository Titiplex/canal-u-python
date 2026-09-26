import logging
from collections import deque

from modules import crawling, parsing
from modules import db_manager as dbm
from modules.visual import loadingbar as lb


def main(max_search_pages=None, db_path=None):
    db = dbm.SQLManager(db_path)
    print(f'Base SQLite : {db.path.resolve()}')
    try:
        # Une panne de recherche ne doit pas empêcher de reprendre la file existante.
        try:
            count = crawling.get_results_count()
        except Exception:
            logging.exception('Impossible de lire le nombre de pages ; reprise de la file existante')
            count = 0
        if max_search_pages is not None:
            count = min(count, max(0, max_search_pages))
        print('Recherche des pages...')
        bar = lb.LoadingBar(count)
        bar.print()
        for i in range(count):
            url = crawling.SEARCH_URL + str(i)
            if not db.is_visited(url):
                try:
                    res = parsing.get_search_items(crawling.crawl(url), url)
                    if not res:
                        raise ValueError('Page de recherche sans résultat parsable')
                    db.save_to_queue(res)
                    db.add_visited(url)
                    db.commit()
                except Exception:
                    db.rollback()
                    logging.exception('Échec de la page de recherche %s', url)
            bar.increment()
            bar.print()
        print('\nTraitement des pages individuelles...')
        queue = deque(db.get_queue())
        bar = lb.LoadingBar(len(queue))
        bar.print()
        while queue:
            url, type_ = queue.popleft()
            try:
                html = crawling.crawl(url)
                # Le parseur de collection gère aussi les pages simples.
                # On récupère simultanément les audios et les éventuels enfants.
                found, metadata = parsing.parse_collection(html, url)
                for audio_url in metadata['audios']:
                    db.create_audio(
                        url, audio_url, metadata['titre'], metadata['desc'],
                        '; '.join(metadata['langues']), metadata['citation'],
                        metadata['cdt'], metadata['lieu'], metadata['doi'],
                    )
                # Parent terminé avant d'ajouter ses enfants : empêche les cycles.
                db.add_visited(url)
                added = db.save_to_queue(metadata['pages'])
                db.remove_from_queue(url)
                # Audio + enfants + visited + suppression sont validés ensemble.
                db.commit()
                queue.extend(added)
                bar.total += len(added)
            except Exception:
                db.rollback()
                logging.exception('Échec de %s ; URL conservée dans la file pour la prochaine exécution', url)
            bar.increment()
            bar.print()
        print(f'\nTerminé. Pages restant à reprendre : {db.get_queue_size()}')
    finally:
        db.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument('--max-search-pages', type=int, default=None)
    parser.add_argument('--db', default=None, help='Chemin de la base SQLite à utiliser')
    args = parser.parse_args()
    logging.basicConfig(level=logging.ERROR)
    main(args.max_search_pages, args.db)
