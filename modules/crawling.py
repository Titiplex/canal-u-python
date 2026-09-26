import re
from urllib.parse import parse_qs, urlsplit

import requests
from bs4 import BeautifulSoup

SEARCH_URL = 'https://www.canal-u.tv/recherche?search_api_fulltext=&op=Submit&page='
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/131.0 Safari/537.36'}


def crawl(url: str) -> str:
    response = requests.get(url, headers=headers, timeout=(10, 30))
    response.raise_for_status()
    # Laisser BeautifulSoup lire le charset HTML si HTTP ne le précise pas.
    if 'charset=' not in response.headers.get('Content-Type', '').lower():
        response.encoding = response.apparent_encoding
    return response.text


def get_results_count() -> int:
    """Nombre de PAGES de recherche (nom conservé pour compatibilité)."""
    soup = BeautifulSoup(crawl(SEARCH_URL + '0'), 'html.parser')
    last = soup.select_one('.pager__item--last a[href]')
    if last:
        return int(parse_qs(urlsplit(last['href']).query)['page'][0]) + 1
    counter = soup.select_one('#global-search-results-counter')
    if counter is None:
        raise ValueError('Compteur de recherche introuvable')
    total = int(re.sub(r'\s+', '', counter.get_text(strip=True)))
    return (total + 11) // 12
