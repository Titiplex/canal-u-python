"""Audit des URL fournies, sans modifier la base SQLite."""
import argparse
import hashlib
import json
from pathlib import Path

from bs4 import BeautifulSoup

from modules import crawling, parsing


def audit(urls, output):
    output.mkdir(parents=True, exist_ok=True)
    results = []
    try:
        for url in urls:
            row = {'url': url}
            if crawling.stopped():
                row.update(status='non_testee', detail='Pause globale après blocage')
                results.append(row)
                continue
            try:
                html = crawling.crawl(url)
                path = output / (hashlib.sha256(url.encode()).hexdigest() + '.html')
                path.write_text(html, encoding='utf-8')
                row['html'] = str(path)
                soup = BeautifulSoup(html, 'html.parser')
                row['ancien_selecteur_present'] = soup.select_one('article.node--view-mode-full') is not None
                _, metadata = parsing.parse_collection(html, url)
                row.update(status=metadata['status'], titre=metadata['titre'],
                           audios=metadata['audios'], pages=metadata['pages'])
            except parsing.UnrecognizedPage as error:
                row.update(status='a_verifier', detail=str(error))
            except crawling.PermanentHTTPError as error:
                row.update(status='introuvable', detail=str(error))
            except Exception as error:
                row.update(status='a_reessayer', detail=str(error))
            results.append(row)
            (output / 'resultats.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
            print(f"{len(results)}/{len(urls)} {row['status']} : {url}", flush=True)
    finally:
        crawling.close()
        (output / 'resultats.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding='utf-8')
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--urls', default=str(Path(__file__).with_name('urls_en_echec.txt')))
    parser.add_argument('--output', default='diagnostic_pages')
    parser.add_argument('--interval', type=float, default=2.0)
    args = parser.parse_args()
    if not 0 <= args.interval < float('inf'):
        parser.error('Intervalle fini >= 0 requis')
    urls = list(dict.fromkeys(
        line.strip() for line in Path(args.urls).read_text(encoding='utf-8').splitlines() if line.strip()))
    crawling.USE_BROWSER_FALLBACK = False
    crawling.configure(args.interval)
    audit(urls, Path(args.output))
