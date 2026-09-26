"""Requests d'abord ; navigateur normal seulement sur une page de challenge."""
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit

import requests
from bs4 import BeautifulSoup

SEARCH_URL = 'https://www.canal-u.tv/recherche?search_api_fulltext=&op=Submit&page='
headers = {'User-Agent': (
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
    'AppleWebKit/537.36 (KHTML, like Gecko) '
    'Chrome/91.0.4472.124 Safari/537.36'
)}
MIN_INTERVAL = 2.0
BROWSER_WAIT_MS = 60000
USE_BROWSER_FALLBACK = True
session = requests.Session()
session.headers.update(headers)
_session_closed = False
_last_request = None
_use_browser = False
_runtime = _browser = _page = None


class RetryLater(RuntimeError):
    def __init__(self, message, delay=300, blocked=True):
        super().__init__(message)
        self.delay = delay
        self.blocked = blocked


def _retry_delay(value, default=300):
    if not value:
        return default
    try:
        return max(default, int(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            return max(default, int((date - datetime.now(timezone.utc)).total_seconds()) + 1)
        except (ValueError, TypeError, OverflowError):
            return default


def _pace():
    global _last_request
    if _last_request is not None:
        remaining = MIN_INTERVAL - (time.monotonic() - _last_request)
        if remaining > 0:
            time.sleep(remaining)
    _last_request = time.monotonic()


def _challenge(html):
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select_one('#global-search-results-counter, article.node--view-mode-full'):
        return False
    text = soup.get_text(' ', strip=True).lower()[:1500]
    return any(word in text for word in ('challenge', 'verify you are human', 'checking your browser', 'just a moment'))


def _browser_fetch(url):
    global _runtime, _browser, _page
    try:
        from playwright.sync_api import sync_playwright, Error, TimeoutError as PlaywrightTimeout
    except ImportError as exc:
        raise RetryLater('Installer playwright puis lancer : python -m playwright install chromium', delay=900) from exc
    try:
        if _browser is None:
            _runtime = sync_playwright().start()
            _browser = _runtime.chromium.launch(headless=False)
            _page = _browser.new_page()
            print('\nNavigateur ouvert. Si une vérification interactive apparaît, intervenir dans sa fenêtre.')
        _pace()
        response = None
        try:
            response = _page.goto(url, wait_until='domcontentloaded', timeout=30000)
        except PlaywrightTimeout:
            # Ne jamais retourner l'ancien document si la navigation a échoué.
            if _page.url != url:
                raise RetryLater('Navigation non terminée vers la page demandée', delay=900)
        if response and (response.status == 429 or response.status >= 500):
            raise RetryLater(f'Navigateur HTTP {response.status}', _retry_delay(response.headers.get('retry-after')))
        if response and response.status >= 400 and not _challenge(_page.content()):
            raise RetryLater(f'Navigateur HTTP {response.status} : accès refusé', delay=900)
        selector = '#global-search-results-counter' if urlsplit(url).path.rstrip(
            '/') == '/recherche' else 'article.node--view-mode-full'
        # Attendre le contenu réel, pas seulement la fin de la page "Challenge".
        # Le code ne clique sur aucun CAPTCHA et ne recharge pas en boucle.
        _page.wait_for_function(
            '''selector => {
                const el = document.querySelector(selector);
                return el && (selector.startsWith('#')
                    ? /^\\d[\\d\\s]*$/.test(el.textContent.trim())
                    : !!document.querySelector('h1'));
            }''', arg=selector, timeout=BROWSER_WAIT_MS)
        html = _page.content()
        if _challenge(html):
            raise RetryLater('Le challenge persiste dans le navigateur', delay=900)
        return html
    except RetryLater:
        raise
    except Error as exc:
        raise RetryLater(f'Navigateur non disponible ou page non résolue : {exc}', delay=900) from exc


def crawl(url: str) -> str:
    global _use_browser, session, _session_closed
    if _use_browser:
        return _browser_fetch(url)
    if _session_closed:
        session = requests.Session()
        session.headers.update(headers)
        _session_closed = False
    _pace()
    response = session.get(url, timeout=(10, 30))
    if 'charset=' not in response.headers.get('Content-Type', '').lower():
        response.encoding = response.apparent_encoding
    html = response.text
    if response.status_code == 429:
        # Un délai explicite doit être respecté, sans essayer un autre client.
        raise RetryLater('HTTP 429 : trop de requêtes', _retry_delay(response.headers.get('Retry-After')))
    if response.status_code in (200, 403, 503) and _challenge(html):
        if response.headers.get('Retry-After'):
            raise RetryLater('Challenge avec délai demandé', _retry_delay(response.headers['Retry-After']))
        if USE_BROWSER_FALLBACK:
            print('\nChallenge détecté : ouverture normale de la page dans Playwright.')
            _use_browser = True
            return _browser_fetch(url)
        raise RetryLater('Challenge détecté, navigateur désactivé', delay=900)
    if response.status_code == 403:
        raise RetryLater('HTTP 403 sans challenge reconnu', _retry_delay(response.headers.get('Retry-After'), 900))
    if response.status_code >= 500:
        raise RetryLater(f'HTTP {response.status} : serveur indisponible',
                         _retry_delay(response.headers.get('Retry-After')))
    response.raise_for_status()
    return html


def close():
    global _runtime, _browser, _page, _use_browser, _last_request, _session_closed
    try:
        if _browser is not None:
            _browser.close()
    finally:
        try:
            if _runtime is not None:
                _runtime.stop()
        finally:
            _runtime = _browser = _page = None
            _use_browser = False
            _last_request = None
            session.close()
            _session_closed = True


def get_results_count() -> int:
    """Nombre de pages de recherche, pas nombre de résultats."""
    soup = BeautifulSoup(crawl(SEARCH_URL + '0'), 'html.parser')
    last = soup.select_one('.pager__item--last a[href]')
    if last:
        return int(parse_qs(urlsplit(last['href']).query)['page'][0]) + 1
    counter = soup.select_one('#global-search-results-counter')
    if counter is None:
        raise ValueError('Compteur de recherche introuvable')
    total = int(re.sub(r'\s+', '', counter.get_text(strip=True)))
    return (total + 11) // 12
