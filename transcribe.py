"""Transcription Whisper locale et sauvegarde dans la table SQLite audios."""
import argparse
import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PureWindowsPath

from modules.process_lock import DatabaseRunLock

PROJECT = Path(__file__).resolve().parent
AUDIO_EXTENSIONS = {'.mp3', '.wav', '.flac', '.m4a', '.aac', '.ogg', '.opus', '.wma', '.aif', '.aiff', '.webm', '.mp4'}
TRANSCRIPTION_COLUMNS = {
    'transcription': 'TEXT',
    'transcription_segments': 'TEXT',
    'transcription_lang': 'TEXT',
    'transcription_model': 'TEXT',
    'transcribed_at': 'TEXT',
}


class Lang(Enum):
    fr = 'fr'
    en = 'en'
    uk = 'uk'  # Convention du script initial : langue inconnue, pas ukrainien.

    def __str__(self):
        return self.value


def prepare_database(conn):
    """Migration additive, compatible SQLite 3.22 et bases existantes."""
    columns = {row[1] for row in conn.execute('PRAGMA table_info(audios)')}
    if not {'id', 'audio_url', 'file_name'}.issubset(columns):
        raise ValueError('La base doit contenir audios(id, audio_url, file_name, ...).')
    with conn:
        for name, kind in TRANSCRIPTION_COLUMNS.items():
            if name not in columns:
                conn.execute(f'ALTER TABLE audios ADD COLUMN {name} {kind}')


def save_transcription(conn, audio_ids, result, model_name, overwrite=False):
    """Sauvegarder une réponse complète ; commit immédiat pour permettre la reprise.

    NULL signifie « pas encore traité ». Un texte vide est une transcription
    terminée (par exemple, un enregistrement sans parole).
    """
    text = result['text'].strip()
    segments = json.dumps(result.get('segments', []), ensure_ascii=False, allow_nan=False)
    values = (text, segments, result.get('language'), str(model_name),
              datetime.now(timezone.utc).isoformat())
    guard = '' if overwrite else ' AND transcription IS NULL'
    count = 0
    with conn:
        for audio_id in audio_ids:
            cursor = conn.execute('''UPDATE audios SET transcription=?, transcription_segments=?,
                                     transcription_lang=?, transcription_model=?, transcribed_at=?
                                     WHERE id=?''' + guard, (*values, audio_id))
            count += cursor.rowcount
    return count


def _basename(value):
    # Les chemins de la base peuvent provenir d'un téléchargement sous Windows.
    return PureWindowsPath(value).name


def _jobs(conn, root, overwrite):
    """Associer uniquement les fichiers référencés ; refuser les noms ambigus."""
    files = {p.resolve() for p in root.rglob('*') if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS}
    by_name = defaultdict(set)
    for path in files:
        by_name[path.name].add(path)

    groups = {}
    for audio_id, url, filename, text in conn.execute(
            'SELECT id, audio_url, file_name, transcription FROM audios ORDER BY id'):
        key = ('url', url) if url and url.strip() else ('id', audio_id)
        group = groups.setdefault(key, {'ids': [], 'references': set()})
        if overwrite or text is None:
            group['ids'].append(audio_id)
        if filename:
            group['references'].add(filename)
    # Le téléchargeur conserve également les chemins dans downloads.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='downloads'").fetchone():
        for url, path in conn.execute("SELECT audio_url, file_path FROM downloads WHERE status='done'"):
            if ('url', url) in groups and path:
                groups[('url', url)]['references'].add(path)

    owners = defaultdict(set)
    for key, group in groups.items():
        for ref in group['references']:
            owners[_basename(ref)].add(key)

    jobs, missing, ambiguous = [], 0, 0
    for key, group in groups.items():
        if not group['ids']:
            continue
        candidates = set()
        for ref in group['references']:
            path = Path(ref.replace('\\', '/'))
            # Résolution exacte, sans dépendre du dossier courant de l'IDE.
            for candidate in (path, root / path, PROJECT / path):
                resolved = candidate.resolve()
                if resolved in files:
                    candidates.add(resolved)
        if not candidates:
            # Dossier déplacé / anciens chemins Windows : nom seulement si unique.
            for ref in group['references']:
                name = _basename(ref)
                if len(owners[name]) == 1:
                    candidates.update(by_name[name])
                elif by_name[name]:
                    candidates.update(by_name[name])
                    # Un seul fichier local ne suffit pas à identifier deux sources.
                    candidates.add(None)
        if len(candidates) > 1 or None in candidates:
            ambiguous += 1
            print(f'Association ambiguë, ignorée : {key[1]}')
        elif candidates:
            jobs.append((next(iter(candidates)), group['ids']))
        else:
            missing += 1

    # Une même copie physique ne peut pas servir à deux sources différentes.
    physical_owners = defaultdict(set)
    for key, group in groups.items():
        for ref in group['references']:
            path = Path(ref.replace('\\', '/'))
            for candidate in (path, root / path, PROJECT / path):
                if candidate.resolve() in files:
                    physical_owners[candidate.resolve()].add(key)
    safe_jobs = []
    for path, ids in jobs:
        if len(physical_owners[path]) > 1:
            ambiguous += 1
            print(f'Fichier référencé par plusieurs sources, ignoré : {path}')
        else:
            safe_jobs.append((path, ids))
    return safe_jobs, missing, ambiguous


def transcribe(dir_path=None, lang=Lang.fr, db_path=None, model_name='turbo',
               overwrite=False, limit=None, device=None, model=None):
    """Transcrire les fichiers de dir_path et enrichir audios dans db_path.

    `model` permet l'injection d'un modèle déjà chargé. Aucun chargement ni
    téléchargement de poids n'a lieu à l'import du script.
    """
    lang = lang if isinstance(lang, Lang) else _parse_lang(lang)
    if limit is not None and limit < 1:
        raise ValueError('La limite doit être >= 1.')
    root = Path(dir_path).resolve() if dir_path is not None else PROJECT / 'corpus_audio'
    db_path = Path(db_path).resolve() if db_path is not None else PROJECT / '.cache/data.db'
    if not root.is_dir():
        raise FileNotFoundError(f'Dossier audio introuvable : {root}')
    if not db_path.is_file():
        raise FileNotFoundError(f'Base existante introuvable : {db_path}')

    # Même verrou que le crawl et le téléchargement : pas de migration concurrente.
    with DatabaseRunLock(db_path):
        conn = sqlite3.connect(str(db_path), timeout=30)
        try:
            prepare_database(conn)
            jobs, missing, ambiguous = _jobs(conn, root, overwrite)
            if limit is not None:
                jobs = jobs[:limit]
            stats = dict(done=0, rows_saved=0, failed=0, missing=missing, ambiguous=ambiguous)
            print(f'À transcrire : {len(jobs)} ; sans fichier local : {missing} ; ambigus : {ambiguous}')
            if not jobs:
                return stats
            if model is None:
                import whisper
                model = whisper.load_model(model_name, device=device)
            language = None if lang == Lang.uk else lang.value
            fp16 = getattr(getattr(model, 'device', None), 'type', None) == 'cuda'
            for index, (file_path, audio_ids) in enumerate(jobs, 1):
                print(f'[{index}/{len(jobs)}] {file_path}')
                try:
                    # Whisper détecte la langue avec language=None ; pas de second décodage manuel.
                    result = model.transcribe(str(file_path), language=language, task='transcribe',
                                              fp16=fp16, verbose=False)
                except Exception as error:
                    stats['failed'] += 1
                    print(f'Échec de transcription : {file_path} : {error}')
                    continue
                # Les erreurs SQLite arrêtent le traitement ; ne pas masquer un disque plein.
                stats['rows_saved'] += save_transcription(conn, audio_ids, result, model_name, overwrite)
                stats['done'] += 1
                print(result['text'].strip())
            print(f"Terminés : {stats['done']} ; lignes enregistrées : {stats['rows_saved']} ; échecs : {stats['failed']}")
            return stats
        finally:
            conn.close()


def _parse_lang(value):
    return Lang.uk if value in ('unknown', 'auto') else Lang(value)


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-d', '--dir', default=None, help='Dossier audio (défaut : corpus_audio du projet)')
    parser.add_argument('--db', default=None, help='Base existante (défaut : .cache/data.db du projet)')
    parser.add_argument('-l', '--lang', type=_parse_lang, default=Lang.fr, choices=list(Lang),
                        help='fr = français, en = anglais, uk/auto/unknown = détection automatique')
    parser.add_argument('--model', default='turbo', help='Nom Whisper ou chemin de poids .pt locaux')
    parser.add_argument('--device', default=None, help='Ex. cpu ou cuda ; automatique si omis')
    parser.add_argument('--overwrite', action='store_true', help='Remplacer les transcriptions existantes')
    parser.add_argument('--limit', type=int, default=None, help='Nombre maximal de fichiers à transcrire')
    return parser


def main():
    args = build_parser().parse_args()
    try:
        stats = transcribe(args.dir, args.lang, args.db, args.model, args.overwrite, args.limit, args.device)
    except KeyboardInterrupt:
        print('\nArrêt demandé ; les transcriptions déjà validées sont conservées.')
        return 130
    return 1 if stats['failed'] or stats['ambiguous'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
