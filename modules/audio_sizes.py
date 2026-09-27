"""Mesure par en-têtes HTTP uniquement ; aucun corps audio lu."""
import re
from urllib.parse import urljoin, urlsplit

import requests

from modules import crawling


def _request(method, url, extra_headers=None):
    # Redirections manuelles : Requests peut consommer leur corps automatiquement.
    session = crawling.get_client().session
    headers = {'Accept-Encoding': 'identity', **(extra_headers or {})}
    for _ in range(9):
        if urlsplit(url).scheme not in {'http', 'https'}:
            raise ValueError('Redirection hors HTTP(S)')
        crawling._gate.pace()
        response = session.request(method, url, headers=headers, stream=True,
                                   allow_redirects=False, timeout=(10, 30))
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        location = response.headers.get('Location')
        response.close()
        if not location:
            raise requests.TooManyRedirects('Redirection sans destination')
        url = urljoin(url, location)
    raise requests.TooManyRedirects('Plus de huit redirections')


def _check_status(response, method):
    code = response.status_code
    delay = response.headers.get('Retry-After')
    if code in {401, 403, 429} or delay:
        raise crawling.RetryLater(f'Mesure audio : HTTP {code}', crawling._retry_delay(delay))
    if code >= 500 and not (method == 'HEAD' and code == 501):
        raise crawling.RetryLater(f'Mesure audio : HTTP {code}', delay=60, blocked=False)


def _audio_response(response):
    mime = response.headers.get('Content-Type', '').split(';')[0].strip().lower()
    encoding = response.headers.get('Content-Encoding', 'identity').lower()
    return (mime.startswith('audio/') or mime in {'application/octet-stream', 'binary/octet-stream'}) and encoding in {
        '', 'identity'}


def _length(response):
    value = response.headers.get('Content-Length', '')
    if response.headers.get('Transfer-Encoding') or not re.fullmatch(r'\d+', value):
        return None
    size = int(value)
    return size if 0 < size <= 2 ** 63 - 1 else None


def probe(url):
    """Retourne connu/inconnu/introuvable ; les erreurs temporaires sont levées."""
    for method in ('HEAD', 'GET'):
        extra = {'Range': 'bytes=0-0'} if method == 'GET' else {}
        with _request(method, url, extra) as response:
            _check_status(response, method)
            code = response.status_code
            result = dict(size_bytes=None, final_url=response.url, method=method,
                          status='unknown', detail=f'HTTP {code}, taille non disponible')
            # Un HEAD peut être mal implémenté, y compris retourner 404.
            if method == 'GET' and code in {404, 410}:
                return {**result, 'status': 'missing'}
            size = None
            if _audio_response(response):
                if code == 200:
                    size = _length(response)
                elif code == 206 and method == 'GET':
                    match = re.fullmatch(r'bytes 0-0/(\d+)', response.headers.get('Content-Range', ''), re.I)
                    if match and 0 < int(match[1]) <= 2 ** 63 - 1:
                        size = int(match[1])
            if size is not None:
                return {**result, 'size_bytes': size, 'status': 'known', 'detail': ''}
            if method == 'GET':
                mime = response.headers.get('Content-Type', '').lower()
                if 'html' in mime:
                    # Ne jamais compter la taille d'un challenge ou d'une page d'erreur.
                    raise crawling.RetryLater('Réponse HTML au lieu de l’audio', delay=300, blocked=False)
                return result


def format_bytes(value):
    return f'{value / 1_000_000_000:.3f} Go ({value / 2 ** 30:.3f} Gio)'


def report(db):
    stats = db.audio_size_summary()
    print(f"Audios distincts (URL) : {stats['total']}")
    print(f"Taille connue : {stats['known']} — {format_bytes(stats['known_bytes'])} — {stats['known_bytes']} octets")
    unknown = {key: value for key, value in stats['statuses'].items() if key != 'known'}
    print(f"Taille inconnue : {stats['total'] - stats['known']} — {unknown}")
    if stats['known'] and stats['known'] < stats['total']:
        estimate = round(stats['known_bytes'] / stats['known'] * stats['total'])
        print('Projection indicative par taille moyenne :', format_bytes(estimate))
        print('Hypothèse : les tailles inconnues ont la même moyenne ; ce n’est pas un volume confirmé.')
    return stats
