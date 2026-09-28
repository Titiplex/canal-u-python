"""Téléchargement streaming, reprise validée et chemins compatibles Windows."""
import hashlib
import json
import os
import re
from pathlib import Path

import requests
import unicodedata

from modules import crawling
from modules.audio_sizes import _request

CHUNK_SIZE = 256 * 1024


class MissingAudio(RuntimeError):
    pass


class InvalidAudio(RuntimeError):
    pass


def slug(value, limit=48):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode().lower()
    value = re.sub(r'[^a-z0-9]+', '-', value).strip('-')[:limit].rstrip('-')
    if value in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(10)), *(f'lpt{i}' for i in range(10))}:
        value = 'lang-' + value
    return value or 'inconnue'


def language_folder(labels):
    aliases = {'fr': 'francais', 'fra': 'francais', 'fre': 'francais', 'french': 'francais',
               'en': 'anglais', 'eng': 'anglais', 'english': 'anglais',
               'es': 'espagnol', 'spa': 'espagnol', 'spanish': 'espagnol',
               'de': 'allemand', 'deu': 'allemand', 'ger': 'allemand',
               'it': 'italien', 'ita': 'italien'}
    names = sorted({aliases.get(slug(v), slug(v)) for label in labels
                    for v in (label or '').split(';') if v.strip()})
    if not names:
        return 'langue_inconnue'
    if len(names) == 1:
        return names[0]
    combined = '-'.join(names)
    if len(combined) > 100:
        combined = combined[:80] + '-' + hashlib.sha256(combined.encode()).hexdigest()[:12]
    return 'multilingue/' + combined


def atomic_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + '.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temp, path)


def read_json(path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {}


def audio_extension(prefix):
    if prefix.startswith(b'ID3'):
        return '.mp3'
    if len(prefix) >= 4:
        # En-tête MPEG audio : synchronisation, version/layer et débits valides.
        for i in range(min(len(prefix) - 3, 4096)):
            a, b, c = prefix[i:i + 3]
            if a == 255 and b & 224 == 224 and b & 24 != 8 and b & 6 != 0 and c & 240 not in {0, 240} and c & 12 != 12:
                return '.mp3'
    if prefix.startswith(b'fLaC'):
        return '.flac'
    if prefix.startswith(b'OggS'):
        return '.ogg'
    if prefix.startswith(b'RIFF') and prefix[8:12] == b'WAVE':
        return '.wav'
    if prefix[4:8] == b'ftyp' and prefix[8:12] in {b'M4A ', b'M4B '}:
        return '.m4a'
    return None


def retry(message, delay=60, blocked=False):
    return crawling.RetryLater(message, delay=delay, blocked=blocked)


def open_audio(url, headers):
    """Même session et même challenge image que le crawler, un seul rafraîchissement."""
    response = _request('GET', url, headers)
    for attempt in range(2):
        code = response.status_code
        delay = response.headers.get('Retry-After')
        if delay or code == 429:
            response.close()
            raise retry(f'HTTP {code}', crawling._retry_delay(delay), True)
        if code in {404, 410}:
            response.close()
            raise MissingAudio(f'HTTP {code} : audio absent')
        mime = response.headers.get('Content-Type', '').lower()
        if 'html' in mime or code in {401, 403, 503}:
            try:
                # Lecture bornée : jamais appeler response.text sur un flux audio.
                data = bytearray()
                for chunk in response.iter_content(4096):
                    data.extend(chunk)
                    if len(data) >= 65536:
                        break
                text = bytes(data).decode('utf-8', errors='replace')
                sample = requests.Response()
                sample.status_code, sample.url = code, response.url
                sample.headers = response.headers.copy()
                sample._content = bytes(data)
                sample.encoding = 'utf-8'
            finally:
                response.close()
            if attempt == 0 and 'bot_challenge.png?' in text:
                replacement = crawling.get_client()._image_challenge(
                    sample, refresh_request=lambda target: _request('GET', target, headers))
                if replacement is not sample:
                    response = replacement
                    continue
            if code in {401, 403} or crawling._challenge(text):
                raise retry('Accès refusé ou challenge persistant', 300, True)
            if code >= 500 or 'unexpected error' in text.lower() or 'try again later' in text.lower():
                raise retry('Erreur serveur à la place de l’audio')
            raise InvalidAudio('Réponse HTML à la place de l’audio ; à examiner')
        if code >= 500 or code in {408, 425}:
            response.close()
            raise retry(f'HTTP {code} : erreur temporaire')
        return response
    raise retry('Challenge non résolu', 300, True)


def paths(job, root):
    key = hashlib.sha256(job['audio_url'].encode()).hexdigest()
    relative = Path(language_folder(job['languages'])) / (slug(job['title'], 40) + '__' + key[:32])
    return root / relative, root / '_state' / (key + '.partial.json'), root / '_state' / (key + '.json')


def existing(job, root):
    _, _, receipt = paths(job, root)
    data = read_json(receipt)
    if data.get('audio_url') != job['audio_url'] or not data.get('complete'):
        return None
    path = (root / data.get('file', '')).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        return None
    if path.stat().st_size != data.get('size_bytes'):
        return None
    with path.open('rb') as stream:
        if audio_extension(stream.read(4096)) is None:
            return None
    return data


def download(job, root):
    root = Path(root).resolve()
    cached = existing(job, root)
    if cached:
        return cached
    base, checkpoint, receipt = paths(job, root)
    base.parent.mkdir(parents=True, exist_ok=True)
    part = base.with_suffix('.part')
    state = read_json(checkpoint)
    offset = part.stat().st_size if part.exists() else 0
    # Seul un ETag fort permet de concaténer une reprise sans mélanger deux versions.
    etag = state.get('etag', '')
    resumable = (offset and state.get('audio_url') == job['audio_url'] and
                 etag.startswith('"') and etag.endswith('"') and not etag.startswith('W/'))
    if not resumable:
        offset = 0
    headers = {'Referer': job['pages'][0]} if job['pages'] else {}
    if offset:
        headers.update(Range=f'bytes={offset}-', **{'If-Range': etag})
    response = open_audio(job['audio_url'], headers)
    # 416 ou identité de représentation modifiée : repartir proprement une seule fois.
    if offset and (response.status_code == 416 or
                   (response.status_code == 206 and
                    (response.headers.get('ETag') != etag or response.url != state.get('final_url')))):
        response.close()
        offset = 0
        headers.pop('Range', None);
        headers.pop('If-Range', None)
        response = open_audio(job['audio_url'], headers)
    with response:
        if response.status_code not in {200, 206}:
            raise InvalidAudio(f'HTTP {response.status_code} non exploitable')
        if response.headers.get('Content-Encoding', 'identity').lower() not in {'', 'identity'}:
            raise InvalidAudio('Encodage compressé inattendu ; taille/reprise non fiables')
        total = None
        if response.status_code == 206:
            match = re.fullmatch(r'bytes (\d+)-(\d+)/(\d+)', response.headers.get('Content-Range', ''), re.I)
            if not match or int(match[1]) != offset or not offset <= int(match[2]) < int(match[3]):
                raise retry('Content-Range invalide ; reprise refusée')
            total = int(match[3])
        else:
            offset = 0  # Serveur ignorant Range ou fichier modifié : écrasement du .part.
            length = response.headers.get('Content-Length', '')
            if length.isdigit() and not response.headers.get('Transfer-Encoding'):
                total = int(length)
        chunks = response.iter_content(CHUNK_SIZE)
        prefix = bytearray()
        for chunk in chunks:
            prefix.extend(chunk)
            if len(prefix) >= 4096:
                break
        first = bytes(prefix)
        stripped = first[:4096].lstrip().lower()
        if stripped.startswith((b'<', b'the website encountered')):
            if b'unexpected error' in stripped or b'try again later' in stripped:
                raise retry('Erreur serveur renvoyée avec un type audio')
            raise InvalidAudio('Texte/HTML renvoyé avec un type audio')
        if offset:
            with part.open('rb') as stream:
                extension = audio_extension(stream.read(4096))
        else:
            extension = audio_extension(first[:4096])
        if extension is None:
            raise InvalidAudio('Signature audio non reconnue ; à examiner')
        atomic_json(checkpoint, dict(audio_url=job['audio_url'], etag=response.headers.get('ETag', ''),
                                     final_url=response.url, total=total))
        with part.open('ab' if offset else 'wb') as stream:
            for chunk in _with_first(first, chunks):
                if crawling.stopped():
                    raise crawling.CrawlCancelled('Téléchargement interrompu ; fichier partiel conservé')
                stream.write(chunk)
        size = part.stat().st_size
        if not size or (total is not None and size != total):
            raise retry(f'Téléchargement incomplet : {size}/{total} octets')
        target = base.with_suffix(extension)
        os.replace(part, target)
        result = dict(audio_url=job['audio_url'], file=target.relative_to(root).as_posix(),
                      size_bytes=size, final_url=response.url, complete=True,
                      title=job['title'], languages=job['languages'], pages=job['pages'])
        atomic_json(receipt, result)
        checkpoint.unlink(missing_ok=True)
        return result


def _with_first(first, chunks):
    if first:
        yield first
    yield from chunks
