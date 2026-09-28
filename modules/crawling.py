"""Requests, traitement du challenge image Canal-U, puis navigateur en secours."""
import logging
import re
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qs, urlsplit, urljoin

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
TRACE_HTTP = False


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


class PermanentHTTPError(RuntimeError):
    """404/410 : URL indisponible, pas de reprise automatique."""


class CrawlCancelled(RuntimeError):
    """Appel non commencé : conserver son URL pour la reprise."""


class RequestGate:
    def __init__(self, interval=2.0):
        self.interval = interval
        self.lock = threading.Lock()
        self.stopped = threading.Event()
        self.last = None

    def pace(self):
        # Un seul départ HTTP autorisé à la fois, tous clients confondus.
        with self.lock:
            if self.stopped.is_set():
                raise CrawlCancelled('Pause globale du crawl')
            delay = 0 if self.last is None else max(0, self.interval - (time.monotonic() - self.last))
            if self.stopped.wait(delay):
                raise CrawlCancelled('Pause globale du crawl')
            self.last = time.monotonic()

    def sleep(self, seconds):
        if self.stopped.wait(seconds):
            raise CrawlCancelled('Pause globale du crawl')


_gate = RequestGate(MIN_INTERVAL)
_local = threading.local()


def configure(interval=None):
    """Appeler avant de lancer les workers, jamais pendant leur travail."""
    global _gate
    _gate = RequestGate(MIN_INTERVAL if interval is None else interval)


def set_interval(interval):
    with _gate.lock:
        _gate.interval = interval


def stop():
    _gate.stopped.set()


def stopped():
    return _gate.stopped.is_set()


def _challenge(html):
    if 'global-search-results-counter' in html or 'node--view-mode-full' in html:
        return False
    soup = BeautifulSoup(html, 'html.parser')
    if soup.select_one('#global-search-results-counter, .node--view-mode-full, .taxonomy-term'):
        return False
    text = soup.get_text(' ', strip=True).lower()[:1500]
    return any(word in text for word in ('challenge', 'verify you are human', 'checking your browser', 'just a moment'))


class Client:
    """Une instance par thread ; fermeture dans le thread qui l'a créée."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(headers)
        self._use_browser = False
        self._runtime = self._browser = self._page = None

    def _image_challenge(self, response, refresh_request=None):
        """Charger l'image puis rafraîchir, comme le demande la page reçue.

        Limité au modèle réellement observé sur Canal-U : aucun JavaScript exécuté,
        aucune URL externe suivie, une seule reprise de la page.
        """
        if response.status_code not in (200, 403, 503) or response.headers.get('Retry-After'):
            return response
        if 'bot_challenge.png?' not in response.text:
            return response
        origin = urlsplit(response.url)
        if origin.scheme != 'https' or origin.netloc != 'www.canal-u.tv':
            return response
        soup = BeautifulSoup(response.text, 'html.parser')
        refresh = soup.find('meta', attrs={'http-equiv': re.compile('^refresh$', re.I)})
        if refresh is None or soup.find('script') is not None:
            return response
        value = refresh.get('content', '').strip()
        if not re.fullmatch(r'\d+(?:\.\d+)?', value) or float(value) > 60:
            return response
        for img in soup.select('img[src]'):
            image_url = urljoin(response.url, img['src'])
            target = urlsplit(image_url)
            if (target.scheme, target.netloc, target.path) == (
                    'https', 'www.canal-u.tv', '/sites/default/files/bot_challenge.png') and target.query:
                break
        else:
            return response
        logging.debug('Challenge image : ressource puis rafraîchissement HTTP')
        _gate.pace()
        image_response = self.session.get(image_url, headers={'Referer': response.url},
                                          timeout=(10, 30), allow_redirects=False)
        try:
            if image_response.status_code == 429 or image_response.headers.get('Retry-After'):
                raise RetryLater('Délai demandé sur la ressource de challenge',
                                 _retry_delay(image_response.headers.get('Retry-After')))
            if image_response.status_code in (401, 403) or image_response.status_code >= 500:
                raise RetryLater(f'Ressource de challenge HTTP {image_response.status_code}')
            # Même si l'image est absente (404), le navigateur fait le rafraîchissement.
            # C'est la réponse à CE rafraîchissement qui décide du succès.
        finally:
            image_response.close()
        _gate.sleep(max(1.0, float(value)))
        if refresh_request is not None:
            # Le téléchargeur fournit un GET en streaming, sans lecture du fichier.
            return refresh_request(response.url)
        _gate.pace()
        return self.session.get(response.url, timeout=(10, 30))

    def _browser_fetch(self, url):
        try:
            from playwright.sync_api import sync_playwright, Error, TimeoutError as PlaywrightTimeout
        except ImportError as exc:
            raise RetryLater('Installer playwright puis lancer : python -m playwright install chromium',
                             delay=900) from exc
        try:
            if self._browser is None:
                self._runtime = sync_playwright().start()
                self._browser = self._runtime.chromium.launch(headless=False)
                self._page = self._browser.new_page()
                print('\nNavigateur ouvert. Si une vérification interactive apparaît, intervenir dans sa fenêtre.')
            _gate.pace()
            response = None
            try:
                response = self._page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except PlaywrightTimeout:
                # Ne jamais retourner l'ancien document si la navigation a échoué.
                if self._page.url != url:
                    raise RetryLater('Navigation non terminée vers la page demandée', delay=900)
            if response and response.status in (404, 410):
                raise PermanentHTTPError(f'HTTP {response.status} : {url}')
            if response and (response.status == 429 or response.status >= 500):
                raise RetryLater(f'Navigateur HTTP {response.status}',
                                 _retry_delay(response.headers.get('retry-after')),
                                 blocked=response.status == 429 or bool(response.headers.get('retry-after')))
            if response and response.status >= 400 and not _challenge(self._page.content()):
                raise RetryLater(f'Navigateur HTTP {response.status} : accès refusé', delay=900)
            selector = '#global-search-results-counter' if urlsplit(url).path.rstrip(
                '/') == '/recherche' else '.node--view-mode-full, .taxonomy-term, main h1, [role="main"] h1'
            # Attendre le contenu réel, pas seulement la fin de la page "Challenge".
            # Le code ne clique sur aucun CAPTCHA et ne recharge pas en boucle.
            self._page.wait_for_function(
                '''selector => {
                    const el = document.querySelector(selector);
                    return el && (selector.startsWith('#')
                        ? /^\\d[\\d\\s]*$/.test(el.textContent.trim())
                        : !!document.querySelector('h1'));
                }''', arg=selector, timeout=BROWSER_WAIT_MS)
            html = self._page.content()
            if _challenge(html):
                raise RetryLater('Le challenge persiste dans le navigateur', delay=900)
            return html
        except RetryLater:
            raise
        except Error as exc:
            raise RetryLater(f'Navigateur non disponible ou page non résolue : {exc}', delay=900) from exc

    def crawl(self, url: str) -> str:
        start = time.monotonic()
        try:
            return self._crawl(url)
        except RetryLater as error:
            if error.blocked:
                stop()
            raise
        finally:
            if TRACE_HTTP:
                print(f'\n[crawl, attentes comprises] {time.monotonic() - start:.2f}s {url}')

    def _crawl(self, url):
        if self._use_browser:
            return self._browser_fetch(url)
        _gate.pace()
        response = self.session.get(url, timeout=(10, 30))
        response = self._image_challenge(response)
        if 'charset=' not in response.headers.get('Content-Type', '').lower():
            response.encoding = response.apparent_encoding
        html = response.text
        if response.status_code in (404, 410):
            raise PermanentHTTPError(f'HTTP {response.status_code} : {url}')
        if response.status_code == 429:
            # Un délai explicite doit être respecté, sans essayer un autre client.
            raise RetryLater('HTTP 429 : trop de requêtes', _retry_delay(response.headers.get('Retry-After')))
        if response.status_code in (200, 403, 503) and _challenge(html):
            if response.headers.get('Retry-After'):
                raise RetryLater('Challenge avec délai demandé', _retry_delay(response.headers['Retry-After']))
            if USE_BROWSER_FALLBACK:
                print('\nChallenge détecté : ouverture normale de la page dans Playwright.')
                self._use_browser = True
                return self._browser_fetch(url)
            raise RetryLater('Challenge détecté, navigateur désactivé', delay=900)
        if response.status_code == 403:
            raise RetryLater('HTTP 403 sans challenge reconnu', _retry_delay(response.headers.get('Retry-After'), 900))
        if response.status_code >= 500:
            raise RetryLater(f'HTTP {response.status_code} : serveur indisponible',
                             _retry_delay(response.headers.get('Retry-After')),
                             blocked=bool(response.headers.get('Retry-After')))
        response.raise_for_status()
        return html

    def close(self):
        try:
            if self._browser is not None:
                self._browser.close()
        finally:
            try:
                if self._runtime is not None:
                    self._runtime.stop()
            finally:
                self._runtime = self._browser = self._page = None
                self._use_browser = False
                self.session.close()


def get_client():
    if not hasattr(_local, 'client'):
        _local.client = Client()
    return _local.client


def crawl(url: str) -> str:
    return get_client().crawl(url)


def close():
    client = getattr(_local, 'client', None)
    if client is not None:
        try:
            client.close()
        finally:
            del _local.client


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
