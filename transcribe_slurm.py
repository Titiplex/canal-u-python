"""Lots Whisper fixes pour Slurm : prepare, run (sans SQLite), puis merge."""
import argparse
import contextlib
import hashlib
import heapq
import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from transcribe import Lang, PROJECT, _jobs, _parse_lang, prepare_database
from modules.process_lock import DatabaseRunLock


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic_json(path, data):
    # Sérialiser avant toute modification ; refuser NaN/Infinity.
    encoded = json.dumps(data, ensure_ascii=False, allow_nan=False)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, suffix='.tmp', dir=str(path.parent))
    temporary = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temporary), str(path))
    finally:
        if temporary.exists():
            temporary.unlink()


def _read_json(path):
    with Path(path).open(encoding='utf-8') as stream:
        return json.load(stream)


def _read_plan(plan_dir):
    directory = Path(plan_dir).resolve()
    meta = _read_json(directory / 'plan.json')
    if meta['version'] != 1 or not isinstance(meta['num_shards'], int) or meta['num_shards'] < 1:
        raise ValueError('Version ou nombre de lots invalide dans le plan')
    return directory, meta


def _read_shard(directory, meta, index):
    if not 0 <= index < meta['num_shards']:
        raise ValueError(f"Index de lot attendu entre 0 et {meta['num_shards'] - 1}")
    shard = _read_json(directory / 'shards' / f'{index:05d}.json')
    if shard['run_id'] != meta['run_id'] or shard['shard_index'] != index:
        raise ValueError('Le lot ne correspond pas au plan')
    return shard['jobs']


def _assert_file(job):
    stat = Path(job['file_path']).stat()
    # st_dev peut différer entre nœuds pour un même montage réseau.
    if stat.st_size != job['size_bytes'] or stat.st_mtime_ns != job['mtime_ns']:
        raise ValueError('Audio modifié depuis la préparation : ' + job['file_path'])


def prepare(plan_dir, db_path=None, dir_path=None, num_shards=48, lang=Lang.fr,
            model_name='turbo', overwrite=False, limit=None):
    if num_shards < 1 or (limit is not None and limit < 1):
        raise ValueError('Nombre de lots et limite >= 1 requis')
    lang = lang if isinstance(lang, Lang) else _parse_lang(lang)
    directory = Path(plan_dir).resolve()
    db_path = Path(db_path).resolve() if db_path else PROJECT / '.cache/data.db'
    root = Path(dir_path).resolve() if dir_path else PROJECT / 'corpus_audio'
    if directory.exists():
        raise FileExistsError('Choisir un nouveau dossier de lots : ' + str(directory))
    if not db_path.is_file() or not root.is_dir():
        raise FileNotFoundError('Base existante et dossier audio existant requis')
    # Seule la préparation et la fusion accèdent à SQLite, sous le verrou existant.
    with DatabaseRunLock(db_path), contextlib.closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        prepare_database(conn)
        selected, missing, ambiguous = _jobs(conn, root, overwrite)
        if limit is not None:
            selected = selected[:limit]
        bindings = {row[0]: list(row) for row in conn.execute(
            'SELECT id,url,audio_url,file_name FROM audios')}
        jobs = []
        for path, ids in selected:
            stat = path.stat()
            identities = [bindings[audio_id] for audio_id in ids]
            encoded = json.dumps([str(path), identities], ensure_ascii=False).encode('utf-8')
            jobs.append(dict(job_id=hashlib.sha256(encoded).hexdigest(), file_path=str(path),
                             bindings=identities, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))
    if not jobs:
        raise ValueError(f'Aucun audio à répartir ; sans fichier={missing}, ambiguës={ambiguous}')
    # LPT : gros fichiers d'abord, vers le lot ayant le moins d'octets estimés.
    # Approximation de la durée, sans lancer des milliers de ffprobe.
    buckets = [[] for _ in range(num_shards)]
    heap = [(0, index) for index in range(num_shards)]
    heapq.heapify(heap)
    for job in sorted(jobs, key=lambda j: (-j['size_bytes'], j['job_id'])):
        weight, index = heapq.heappop(heap)
        buckets[index].append(job)
        heapq.heappush(heap, (weight + max(1, job['size_bytes']), index))
    meta = dict(version=1, run_id=uuid.uuid4().hex, created_at=_now(), db_path=str(db_path),
                audio_root=str(root), num_shards=num_shards, lang=lang.value, model=str(model_name),
                overwrite=overwrite, total=len(jobs), missing=missing, ambiguous=ambiguous)
    directory.mkdir(parents=True)
    for index, shard_jobs in enumerate(buckets):
        _atomic_json(directory / 'shards' / f'{index:05d}.json',
                     dict(run_id=meta['run_id'], shard_index=index, jobs=shard_jobs))
    # plan.json n'apparaît qu'une fois tous les lots écrits.
    _atomic_json(directory / 'plan.json', meta)
    print(f'Plan : {directory}\nAudios : {len(jobs)} ; lots : {num_shards} ; '
          f'sans fichier : {missing} ; associations ambiguës exclues : {ambiguous}')
    for index, shard_jobs in enumerate(buckets):
        print(f'Lot {index}: {len(shard_jobs)} fichiers ; '
              f"{sum(j['size_bytes'] for j in shard_jobs) / 1e9:.3f} Go")
    return meta


def _validate_result(payload, meta, index, job):
    expected = (meta['run_id'], index, job['job_id'], meta['model'], meta['lang'])
    observed = tuple(payload.get(k) for k in ('run_id', 'shard_index', 'job_id', 'model', 'lang'))
    if observed != expected:
        raise ValueError('Résultat incompatible avec le lot : ' + job['file_path'])
    result = payload['result']
    if not isinstance(result['text'], str) or not isinstance(result['segments'], list):
        raise ValueError('Texte ou segments invalides')
    if result.get('language') is not None and not isinstance(result['language'], str):
        raise ValueError('Langue renvoyée invalide')
    json.dumps(result, allow_nan=False)
    datetime.fromisoformat(payload['completed_at'])
    return result


def _load_model(directory, model_name, device):
    # Slurm/Linux : sérialiser le téléchargement/chargement initial des poids
    # entre workers de ce plan. L'inférence reste entièrement parallèle.
    import fcntl
    import whisper
    lock_path = directory / 'locks/model-load.lock'
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a+b') as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        return whisper.load_model(model_name, device=device)


def run_shard(plan_dir, shard_index, device='cuda', model=None, limit=None):
    """Ne lit ni ne modifie SQLite. Résultat atomique et reprise par audio."""
    directory, meta = _read_plan(plan_dir)
    jobs = _read_shard(directory, meta, shard_index)
    if limit is not None and limit < 1:
        raise ValueError('Limite >= 1 requise')
    result_dir = directory / 'results' / f'{shard_index:05d}'
    stats = dict(done=0, resumed=0, failed=0, total=len(jobs))
    # Verrou indépendant par lot ; aucun verrou sur la base commune.
    with DatabaseRunLock(directory / 'locks' / f'shard-{shard_index:05d}'):
        pending = []
        for job in jobs:
            success = result_dir / (job['job_id'] + '.json')
            if success.exists():
                _validate_result(_read_json(success), meta, shard_index, job)
                _assert_file(job)
                stats['resumed'] += 1
            else:
                pending.append(job)
        if limit is not None:
            pending = pending[:limit]
        if pending and model is None:
            model = _load_model(directory, meta['model'], device)
        fp16 = getattr(getattr(model, 'device', None), 'type', None) == 'cuda'
        language = None if meta['lang'] == Lang.uk.value else meta['lang']
        for number, job in enumerate(pending, 1):
            print(f"Lot {shard_index} [{number}/{len(pending)}] {job['file_path']}", flush=True)
            try:
                _assert_file(job)
                result = model.transcribe(job['file_path'], language=language,
                                          task='transcribe', fp16=fp16, verbose=False)
                _assert_file(job)
                payload = dict(run_id=meta['run_id'], shard_index=shard_index,
                               job_id=job['job_id'], model=meta['model'], lang=meta['lang'],
                               completed_at=_now(), result=dict(text=result['text'],
                               segments=result.get('segments', []), language=result.get('language')))
                _validate_result(payload, meta, shard_index, job)
            except Exception as error:
                stats['failed'] += 1
                _atomic_json(result_dir / (job['job_id'] + '.error.json'),
                             dict(job_id=job['job_id'], at=_now(), file=job['file_path'], error=str(error)))
                print(f"Échec : {job['file_path']} : {error}", flush=True)
                continue
            # Une erreur de disque pendant la sauvegarde arrête le worker.
            _atomic_json(result_dir / (job['job_id'] + '.json'), payload)
            error_file = result_dir / (job['job_id'] + '.error.json')
            if error_file.exists():
                error_file.unlink()
            stats['done'] += 1
        stats['remaining'] = stats['total'] - stats['resumed'] - stats['done']
        _atomic_json(directory / 'status' / f'{shard_index:05d}.json', stats)
    print(f'Lot {shard_index}: {stats}', flush=True)
    return stats


def _import_one(conn, meta, job, result, completed_at):
    """Identité de chaque ligne vérifiée sous transaction d'écriture, puis commit."""
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if conn.execute('SELECT 1 FROM transcription_array_imports WHERE run_id=? AND job_id=?',
                        (meta['run_id'], job['job_id'])).fetchone():
            return 0, True
        for expected in job['bindings']:
            current = conn.execute('SELECT id,url,audio_url,file_name FROM audios WHERE id=?',
                                   (expected[0],)).fetchone()
            if current is None or list(current) != expected:
                raise ValueError('Entrée SQLite supprimée ou modifiée depuis le plan : ' + str(expected[0]))
        values = (result['text'].strip(), json.dumps(result['segments'], ensure_ascii=False, allow_nan=False),
                  result.get('language'), meta['model'], completed_at)
        guard = '' if meta['overwrite'] else ' AND transcription IS NULL'
        count = 0
        for expected in job['bindings']:
            count += conn.execute('''UPDATE audios SET transcription=?,transcription_segments=?,
                                     transcription_lang=?,transcription_model=?,transcribed_at=?
                                     WHERE id=?''' + guard, (*values, expected[0])).rowcount
        conn.execute('INSERT INTO transcription_array_imports VALUES (?,?,?,?)',
                     (meta['run_id'], job['job_id'], _now(), count))
        return count, False


def merge(plan_dir, db_path=None):
    directory, meta = _read_plan(plan_dir)
    db_path = Path(db_path).resolve() if db_path else Path(meta['db_path'])
    if not db_path.is_file():
        raise FileNotFoundError('Base existante requise : ' + str(db_path))
    stats = dict(imported=0, already_imported=0, rows_saved=0, missing=0, rejected=0, retry_shards=[])
    with DatabaseRunLock(db_path), contextlib.closing(sqlite3.connect(str(db_path), timeout=30)) as conn:
        prepare_database(conn)
        conn.execute('''CREATE TABLE IF NOT EXISTS transcription_array_imports
                        (run_id TEXT NOT NULL,job_id TEXT NOT NULL,imported_at TEXT NOT NULL,
                         rows_saved INTEGER NOT NULL,PRIMARY KEY(run_id,job_id))''')
        conn.commit()
        for index in range(meta['num_shards']):
            incomplete = False
            for job in _read_shard(directory, meta, index):
                success = directory / 'results' / f'{index:05d}' / (job['job_id'] + '.json')
                if not success.exists():
                    stats['missing'] += 1
                    incomplete = True
                    continue
                try:
                    payload = _read_json(success)
                    result = _validate_result(payload, meta, index, job)
                    # Si déjà fusionné, aucun nouveau contrôle de l'audio n'est nécessaire.
                    previous = conn.execute('SELECT 1 FROM transcription_array_imports WHERE run_id=? AND job_id=?',
                                            (meta['run_id'], job['job_id'])).fetchone()
                    if previous:
                        stats['already_imported'] += 1
                        continue
                    _assert_file(job)
                    count, already = _import_one(conn, meta, job, result, payload['completed_at'])
                except (ValueError, KeyError, TypeError, OSError) as error:
                    stats['rejected'] += 1
                    print(f"Résultat refusé : {job['file_path']} : {error}", flush=True)
                    continue
                stats['already_imported' if already else 'imported'] += 1
                stats['rows_saved'] += count
            if incomplete:
                stats['retry_shards'].append(index)
    print(f'Fusion : {stats}')
    if stats['retry_shards']:
        print('Lots incomplets à relancer : ' + ','.join(map(str, stats['retry_shards'])))
    return stats


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('prepare', help='Créer une répartition fixe avant sbatch')
    p.add_argument('--plan', required=True)
    p.add_argument('--db', default=None)
    p.add_argument('-d', '--dir', default=None)
    p.add_argument('--num-shards', type=int, default=48)
    p.add_argument('-l', '--lang', type=_parse_lang, default=Lang.fr, choices=list(Lang))
    p.add_argument('--model', default='turbo')
    p.add_argument('--overwrite', action='store_true')
    p.add_argument('--limit', type=int, default=None, help='Limite globale avant répartition')
    p = commands.add_parser('run', help='Exécuter un seul lot, sans connexion SQLite')
    p.add_argument('--plan', required=True)
    p.add_argument('--shard-index', type=int, default=None, help='Défaut : SLURM_ARRAY_TASK_ID')
    p.add_argument('--device', default='cuda')
    p.add_argument('--limit', type=int, default=None, help='Limiter les nouvelles tentatives de ce lot')
    p = commands.add_parser('merge', help='Importer les résultats dans SQLite, sans GPU')
    p.add_argument('--plan', required=True)
    p.add_argument('--db', default=None, help='Défaut : base enregistrée dans le plan')
    return parser


def main():
    args = build_parser().parse_args()
    try:
        if args.command == 'prepare':
            prepare(args.plan, args.db, args.dir, args.num_shards, args.lang,
                    args.model, args.overwrite, args.limit)
            return 0
        if args.command == 'run':
            index = args.shard_index
            if index is None:
                if 'SLURM_ARRAY_TASK_ID' not in os.environ:
                    raise ValueError('--shard-index ou SLURM_ARRAY_TASK_ID requis')
                index = int(os.environ['SLURM_ARRAY_TASK_ID'])
            stats = run_shard(args.plan, index, args.device, limit=args.limit)
            return 1 if stats['remaining'] else 0
        stats = merge(args.plan, args.db)
        return 1 if stats['missing'] or stats['rejected'] else 0
    except KeyboardInterrupt:
        print('\nArrêt demandé ; résultats complets déjà sauvegardés conservés.')
        return 130
    except (OSError, ValueError, KeyError, TypeError, RuntimeError, sqlite3.Error) as error:
        print('Erreur : ' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
