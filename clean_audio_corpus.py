#!/usr/bin/env python3
"""Répare un corpus Canal-U et ses références SQLite (Python >= 3.8).

Simulation : python clean_audio_corpus.py --db data.db --root corpus_audio
Application : même commande avec --apply
Sans réencodage : ajouter --aac-action rename (AAC conservé, extension corrigée)
Après interruption : mêmes --db/--root, puis --recover /chemin/sauvegarde

Seules les images reconnues sont retirées et les AAC traités. Les fichiers
inconnus restent en place. Les originaux et les reçus sont sauvegardés.
FFprobe est requis ; FFmpeg avec libmp3lame est requis pour convertir.
"""
import argparse
import contextlib
import csv
import hashlib
import json
import math
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from pathlib import Path
from urllib.parse import quote


MEDIA_EXTENSIONS = {'.mp3', '.aac', '.m4a', '.m4b', '.mp4', '.wav', '.flac',
                    '.ogg', '.oga', '.opus', '.png', '.jpg', '.jpeg', '.gif',
                    '.webp', '.bmp', '.tif', '.tiff', '.avif', '.heic'}
IMAGE_CONTAINERS = {'png_pipe', 'jpeg_pipe', 'webp_pipe', 'bmp_pipe', 'tiff_pipe',
                    'gif', 'apng', 'ico', 'image2', 'image2pipe', 'avif', 'heif'}


def within(path, root):
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


def json_read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def json_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.' + path.name, dir=str(path.parent))
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(str(temp), str(path))
    finally:
        if temp.exists():
            temp.unlink()


def copy_synced(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(str(source), str(target))
    with target.open('rb') as stream:
        os.fsync(stream.fileno())


class RunLock:
    """Même fichier et même verrou que modules/process_lock.py du téléchargeur."""
    def __init__(self, path):
        self.path = Path(str(path.resolve()) + '.crawl.lock')

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        self.stream.seek(0, 2)
        if not self.stream.tell():
            self.stream.write(b'0')
            self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError('Un crawler/nettoyeur utilise déjà ce chemin : ' + str(self.path))
        return self

    def __exit__(self, *args):
        self.stream.close()


def stamp(path):
    stat = path.stat()
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns]


def run_command(command, timeout):
    try:
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        raise RuntimeError('Délai dépassé : ' + command[0])
    if result.returncode:
        raise RuntimeError(result.stderr.decode('utf-8', errors='replace')[-3000:]
                           or 'Échec de ' + command[0])
    return result.stdout


def ff_options():
    # Pas de requêtes réseau si un faux fichier audio contient une liste de lecture.
    return ['-v', 'error', '-protocol_whitelist', 'file,pipe', '-threads', '1']


def probe(path, ffprobe, timeout):
    raw = run_command([ffprobe, *ff_options(), '-show_entries',
                       'stream=index,codec_type,codec_name:stream_disposition=attached_pic:format=format_name,duration',
                       '-of', 'json', str(path)], timeout)
    return json.loads(raw.decode('utf-8'))


def picture_signature(prefix):
    signatures = [(b'\x89PNG\r\n\x1a\n', 'PNG'), (b'\xff\xd8\xff', 'JPEG'),
                  (b'GIF87a', 'GIF'), (b'GIF89a', 'GIF'), (b'BM', 'BMP'),
                  (b'II*\x00', 'TIFF'), (b'MM\x00*', 'TIFF')]
    for signature, name in signatures:
        if prefix.startswith(signature):
            return name
    if prefix.startswith(b'RIFF') and prefix[8:12] == b'WEBP':
        return 'WebP'
    return None


def inspect_file(path, ffprobe, timeout):
    result = dict(path=str(path), kind='unknown', detail='', stamp=None, size_bytes=0)
    try:
        result['stamp'] = stamp(path)
        result['size_bytes'] = result['stamp'][2]
        with path.open('rb') as stream:
            picture = picture_signature(stream.read(32))
        data = probe(path, ffprobe, timeout)
        audios = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
        containers = set(data.get('format', {}).get('format_name', '').split(','))
        result['probe'] = data
        if not audios:
            if containers & IMAGE_CONTAINERS:
                result.update(kind='image', detail=picture or 'Image détectée par ffprobe')
            else:
                result['detail'] = 'Aucun flux audio ; conservé pour examen'
        elif len(audios) != 1:
            result['detail'] = 'Plusieurs flux audio ; traitement manuel requis'
        elif any(s.get('codec_type') == 'video' and not s.get('disposition', {}).get('attached_pic')
                 for s in data.get('streams', [])):
            result.update(kind='other_audio', detail='Audio avec vidéo ; fichier conservé')
        elif audios[0].get('codec_name') == 'aac':
            result.update(kind='aac', detail='AAC : ' + ','.join(sorted(containers)))
        elif audios[0].get('codec_name') == 'mp3' and 'mp3' in containers:
            result.update(kind='mp3', detail='MP3 détecté')
        else:
            result.update(kind='other_audio', detail='Audio conservé : ' + str(audios[0].get('codec_name')))
    except (OSError, RuntimeError, ValueError) as error:
        result['detail'] = str(error)
    return result


def schema(conn):
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'audios' not in tables:
        raise RuntimeError('La base ne contient pas la table audios de Canal-U')
    required = {'audios': {'audio_url', 'file_name', 'url', 'title', 'lang'},
                'downloads': {'output_root', 'audio_url', 'status', 'file_path', 'size_bytes',
                              'attempts', 'next_attempt', 'detail', 'updated_at'},
                'audio_sizes': {'audio_url', 'size_bytes', 'status', 'detail', 'checked_at',
                                'next_attempt', 'final_url', 'method', 'attempts'}}
    for table, columns in required.items():
        if table in tables:
            actual = {r[1] for r in conn.execute('PRAGMA table_info(' + table + ')')}
            if not columns <= actual:
                raise RuntimeError('Schéma incompatible : ' + table + ', colonnes absentes : '
                                   + ', '.join(sorted(columns - actual)))
    return tables


def mapped_path(value, root):
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    if path.is_symlink() or not within(path, root):
        return None
    return path.resolve()


def collect(conn, tables, root):
    links, metadata = {}, {}
    def link(value, url):
        path = mapped_path(value, root)
        if path and path.suffix.lower() != '.part' and '_state' not in path.relative_to(root).parts:
            links.setdefault(path, set()).add(url)
    for row in conn.execute('SELECT audio_url,file_name,url,title,lang FROM audios'):
        url, filename, page, title, lang = row
        if not url:
            continue
        info = metadata.setdefault(url, dict(title=title or 'audio', pages=[], languages=[], local_refs={}))
        local = mapped_path(filename, root)
        if local:
            info['local_refs'].setdefault(str(local), []).append(filename)
        for key, value in [('pages', page), ('languages', lang)]:
            if value and value not in info[key]:
                info[key].append(value)
        link(filename, url)
    if 'downloads' in tables:
        for url, filename in conn.execute('SELECT audio_url,file_path FROM downloads WHERE output_root=?',
                                          (str(root),)):
            link(filename, url)
    for receipt in sorted((root / '_state').glob('*.json')):
        if receipt.name.endswith('.partial.json') or receipt.name == 'cleanup_pending.json':
            continue
        try:
            data = json_read(receipt)
            if data.get('audio_url') and data.get('file'):
                link(data['file'], data['audio_url'])
        except (ValueError, OSError, AttributeError):
            print('Reçu non lisible, conservé : ' + str(receipt), file=sys.stderr)
    hashes = {hashlib.sha256(url.encode('utf-8')).hexdigest()[:32]: url for url in metadata}
    candidates = set(links)
    for base, dirs, files in os.walk(str(root), followlinks=False):
        dirs[:] = sorted(d for d in dirs if d != '_state' and not (Path(base) / d).is_symlink())
        for name in sorted(files):
            path = Path(base) / name
            if path.suffix.lower() in MEDIA_EXTENSIONS and not path.is_symlink():
                path = path.resolve()
                candidates.add(path)
                suffix = path.stem.rsplit('__', 1)[-1]
                if suffix in hashes:
                    links.setdefault(path, set()).add(hashes[suffix])
    files = sorted(p for p in candidates if p.is_file())
    return files, links, metadata


def ensure_write_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS downloads (
                    output_root TEXT NOT NULL, audio_url TEXT NOT NULL, status TEXT NOT NULL,
                    file_path TEXT, size_bytes INTEGER, attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL, detail TEXT, updated_at REAL NOT NULL,
                    PRIMARY KEY(output_root,audio_url))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS corpus_cleanup_commits (
                    run_id TEXT NOT NULL, item_id TEXT NOT NULL, committed_at REAL NOT NULL,
                    PRIMARY KEY(run_id,item_id))''')
    conn.commit()


def set_download(conn, root, url, status, path, size, detail):
    values = (status, path, size, detail, time.time(), str(root), url)
    cur = conn.execute('''UPDATE downloads SET status=?,file_path=?,size_bytes=?,detail=?,updated_at=?,
                          attempts=0,next_attempt=NULL WHERE output_root=? AND audio_url=?''', values)
    if not cur.rowcount:
        conn.execute('''INSERT INTO downloads(status,file_path,size_bytes,detail,updated_at,output_root,
                          audio_url,attempts,next_attempt) VALUES (?,?,?,?,?,?,?,0,NULL)''', values)


def backup_item(path, target, urls, root, backup, item_id):
    original = backup / 'originals' / path.relative_to(root)
    if original.exists():
        raise RuntimeError('Sauvegarde déjà présente pour ce fichier')
    copy_synced(path, original)
    snapshots = []
    part = path.with_suffix('.part')
    if part.exists():
        if part.is_symlink() or not part.is_file():
            raise RuntimeError('Fichier partiel non régulier : ' + str(part))
        relative = part.relative_to(root)
        copy_synced(part, backup / 'metadata' / relative)
        snapshots.append(dict(path=relative.as_posix(), present=True))
    for url in sorted(urls):
        key = hashlib.sha256(url.encode('utf-8')).hexdigest()
        for relative in [Path('_state') / (key + '.json'), Path('_state') / (key + '.partial.json')]:
            local = root / relative
            if local.is_symlink():
                raise RuntimeError('Reçu symbolique refusé : ' + str(local))
            saved = backup / 'metadata' / relative
            present = local.exists()
            if present:
                if not saved.exists():
                    copy_synced(local, saved)
            snapshots.append(dict(path=relative.as_posix(), present=present))
    journal = dict(item_id=item_id, source=path.relative_to(root).as_posix(),
                   target=target.relative_to(root).as_posix() if target else None,
                   snapshots=snapshots, status='prepared')
    journal_path = backup / 'jobs' / (item_id + '.json')
    json_write(journal_path, journal)
    return journal_path, journal


def restore_item(journal, root, backup):
    source = root / journal['source']
    target = root / journal['target'] if journal['target'] else None
    if not within(source, root) or (target and not within(target, root)):
        raise RuntimeError('Chemin hors corpus dans le journal')
    copy_synced(backup / 'originals' / journal['source'], source)
    if target and target != source and target.exists():
        target.unlink()
    for snapshot in journal['snapshots']:
        local = root / snapshot['path']
        if not within(local, root):
            raise RuntimeError('Reçu hors corpus dans le journal')
        if snapshot['present']:
            copy_synced(backup / 'metadata' / snapshot['path'], local)
        elif local.exists():
            local.unlink()


def recover(conn, root, db, backup):
    manifest = json_read(backup / 'run.json')
    if manifest['root'] != str(root) or manifest['db'] != str(db):
        raise RuntimeError('La sauvegarde ne correspond pas à ces --db et --root')
    tables = schema(conn)
    for journal_path in sorted((backup / 'jobs').glob('*.json')):
        journal = json_read(journal_path)
        if journal['status'] in {'done', 'rolled_back'}:
            continue
        committed = ('corpus_cleanup_commits' in tables and conn.execute(
            'SELECT 1 FROM corpus_cleanup_commits WHERE run_id=? AND item_id=?',
            (manifest['run_id'], journal['item_id'])).fetchone())
        if committed:
            journal['status'] = 'done'
        else:
            restore_item(journal, root, backup)
            journal['status'] = 'rolled_back'
        json_write(journal_path, journal)
    # Une interruption pendant FFmpeg peut précéder la création du journal par fichier.
    for base, dirs, files in os.walk(str(root), followlinks=False):
        dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink()]
        for name in files:
            if name.startswith('.cleanup-' + manifest['run_id'] + '-') and name.endswith('.mp3'):
                temp = Path(base) / name
                if not temp.is_symlink():
                    temp.unlink()
    pending = root / '_state' / 'cleanup_pending.json'
    if pending.exists() and json_read(pending).get('backup') == str(backup):
        pending.unlink()
    print('Récupération terminée ; opérations validées conservées, opérations interrompues annulées.')


def converted_temp(path, ffmpeg, ffprobe, args, run_id):
    fd, name = tempfile.mkstemp(prefix='.cleanup-' + run_id + '-', suffix='.mp3', dir=str(path.parent))
    os.close(fd)
    temp = Path(name)
    try:
        run_command([ffmpeg, '-nostdin', '-y', '-xerror', *ff_options(), '-err_detect', 'explode',
                     '-i', str(path), '-map', '0:a:0', '-vn', '-sn', '-dn',
                     '-c:a', 'libmp3lame', '-q:a', str(args.quality), '-threads', '1',
                     '-f', 'mp3', str(temp)], args.convert_timeout)
        data = probe(temp, ffprobe, args.probe_timeout)
        audios = [s for s in data.get('streams', []) if s.get('codec_type') == 'audio']
        duration = float(data.get('format', {}).get('duration', 0))
        if len(audios) != 1 or audios[0].get('codec_name') != 'mp3' or not math.isfinite(duration) or duration <= 0:
            raise RuntimeError('Sortie MP3 non validée')
        run_command([ffmpeg, '-nostdin', '-xerror', *ff_options(), '-err_detect', 'explode',
                     '-i', str(temp), '-map', '0:a:0', '-f', 'null', '-'], args.convert_timeout)
        with temp.open('rb') as stream:
            os.fsync(stream.fileno())
        return temp
    except BaseException:
        if temp.exists():
            temp.unlink()
        raise


def apply_item(entry, urls, metadata, conn, tables, root, backup, run_id, args, ffmpeg, ffprobe):
    path = Path(entry['path'])
    if stamp(path) != entry['stamp']:
        raise RuntimeError('Fichier modifié depuis son inspection ; opération refusée')
    action = 'remove_image' if entry['kind'] == 'image' else args.aac_action
    target = None if action == 'remove_image' else path.with_suffix('.mp3' if action == 'convert' else '.aac')
    if target and target != path and target.exists():
        raise RuntimeError('Collision : la destination existe déjà : ' + str(target))
    if action == 'rename' and target == path:
        return 'already_aac'
    temp, journal_path, journal, committed = None, None, None, False
    item_id = uuid.uuid4().hex
    try:
        if action == 'convert':
            print('Conversion : ' + str(path), flush=True)
            temp = converted_temp(path, ffmpeg, ffprobe, args, run_id)
        if stamp(path) != entry['stamp']:
            raise RuntimeError('Fichier modifié pendant la conversion ; opération refusée')
        journal_path, journal = backup_item(path, target, urls, root, backup, item_id)
        conn.execute('BEGIN IMMEDIATE')
        if action == 'remove_image':
            path.unlink()
        elif action == 'rename':
            os.replace(str(path), str(target))
        else:
            os.replace(str(temp), str(target))
            temp = None
            if target != path:
                path.unlink()
        for url in sorted(urls):
            key = hashlib.sha256(url.encode('utf-8')).hexdigest()
            receipt_path = root / '_state' / (key + '.json')
            partial = root / '_state' / (key + '.partial.json')
            refs = list(dict.fromkeys([str(path), path.relative_to(root).as_posix()]
                        + metadata.get(url, {}).get('local_refs', {}).get(str(path), [])))
            ref_clause = 'file_name IN (' + ','.join('?' for _ in refs) + ')'
            if action == 'remove_image':
                set_download(conn, root, url, 'invalid', None, None, 'Image retirée par clean_audio_corpus')
                conn.execute("UPDATE audios SET file_name='' WHERE audio_url=? AND " + ref_clause,
                             (url, *refs))
                # Invalider la fausse taille distante seulement sans autre copie validée.
                other = conn.execute("SELECT 1 FROM downloads WHERE audio_url=? AND output_root!=? AND status='done'",
                                     (url, str(root))).fetchone()
                if 'audio_sizes' in tables and not other:
                    conn.execute("""UPDATE audio_sizes SET size_bytes=NULL,status='invalid',next_attempt=NULL,
                                    detail='Réponse image retirée du corpus',checked_at=? WHERE audio_url=?""",
                                 (time.time(), url))
                if receipt_path.exists():
                    receipt_path.unlink()
            else:
                size = target.stat().st_size
                try:
                    receipt = json_read(receipt_path)
                except (OSError, ValueError):
                    receipt = {}
                if not isinstance(receipt, dict):
                    receipt = {}
                info = metadata.get(url, dict(title=path.stem, pages=[], languages=[]))
                receipt.update(audio_url=url, complete=True, file=target.relative_to(root).as_posix(),
                               size_bytes=size, title=receipt.get('title', info['title']),
                               pages=receipt.get('pages', info['pages']), languages=receipt.get('languages', info['languages']))
                if not receipt.get('final_url'):
                    remote = conn.execute('SELECT final_url FROM audio_sizes WHERE audio_url=?', (url,)).fetchone() if 'audio_sizes' in tables else None
                    receipt['final_url'] = (remote[0] if remote else None) or url
                receipt['local_cleanup'] = dict(action=action, source_codec='aac',
                                               source_size_bytes=entry['size_bytes'], at=time.time(),
                                               quality=args.quality if action == 'convert' else None)
                json_write(receipt_path, receipt)
                set_download(conn, root, url, 'done', str(target), size, 'AAC traité : ' + action)
                conn.execute("UPDATE audios SET file_name=? WHERE audio_url=? AND (" + ref_clause
                             + " OR file_name='' OR file_name IS NULL)", (str(target), url, *refs))
                # audio_sizes décrit la source distante AAC : ne pas la remplacer par la taille du MP3 local.
            if partial.exists():
                partial.unlink()
        part = path.with_suffix('.part')
        if part.exists():
            part.unlink()
        conn.execute('INSERT INTO corpus_cleanup_commits(run_id,item_id,committed_at) VALUES (?,?,?)',
                     (run_id, item_id, time.time()))
        conn.commit()
        committed = True
        journal['status'] = 'done'
        json_write(journal_path, journal)
        return action
    except BaseException:
        if not committed:
            conn.rollback()
            if journal is not None:
                restore_item(journal, root, backup)
                journal['status'] = 'rolled_back'
                json_write(journal_path, journal)
        raise
    finally:
        if temp and temp.exists():
            temp.unlink()


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', required=True, type=Path)
    parser.add_argument('--root', required=True, type=Path, help='Même dossier absolu que --output du téléchargeur')
    parser.add_argument('--apply', action='store_true', help='Appliquer ; sans cette option : simulation')
    parser.add_argument('--aac-action', choices=['convert', 'rename', 'keep'], default='convert')
    parser.add_argument('--quality', type=int, default=2, choices=range(10), help='Qualité libmp3lame 0..9 (défaut 2)')
    parser.add_argument('--workers', type=int, default=4, help='Inspections parallèles ; conversions séquentielles')
    parser.add_argument('--probe-timeout', type=float, default=30)
    parser.add_argument('--convert-timeout', type=float, default=7200)
    parser.add_argument('--report', type=Path, default=Path('audio_cleanup_report.csv'))
    parser.add_argument('--backup-dir', type=Path, help='Nouveau dossier hors corpus ; créé lors de --apply')
    parser.add_argument('--recover', type=Path, help='Terminer la récupération d’une sauvegarde interrompue')
    return parser


def inspections(pool, files, ffprobe, timeout, window):
    """Limiter les tâches en vol pour que Ctrl+C n'attende pas tout le corpus."""
    pending = deque()
    iterator = iter(files)
    try:
        for _ in range(window):
            path = next(iterator, None)
            if path is not None:
                pending.append(pool.submit(inspect_file, path, ffprobe, timeout))
        while pending:
            yield pending.popleft().result()
            path = next(iterator, None)
            if path is not None:
                pending.append(pool.submit(inspect_file, path, ffprobe, timeout))
    finally:
        for future in pending:
            future.cancel()


def run(args):
    root, db = args.root.resolve(), args.db.resolve()
    if not root.is_dir() or not db.is_file():
        raise RuntimeError('Le dossier du corpus et la base doivent déjà exister')
    if args.workers < 1 or any(not math.isfinite(t) or t <= 0 for t in [args.probe_timeout, args.convert_timeout]):
        raise RuntimeError('Workers >= 1 et délais finis > 0 requis')
    # L’inspection n'écrit pas dans SQLite ; les verrous évitent de scanner pendant un download.
    with RunLock(db), RunLock(root / '_state' / 'downloads'):
        uri = 'file:' + quote(str(db), safe='/') + ('?mode=rw' if args.apply or args.recover else '?mode=ro')
        with contextlib.closing(sqlite3.connect(uri, uri=True, timeout=30)) as conn:
            tables = schema(conn)
            pending = root / '_state' / 'cleanup_pending.json'
            if args.recover:
                recover(conn, root, db, args.recover.resolve())
                return 0
            if pending.exists():
                raise RuntimeError('Nettoyage interrompu : utiliser --recover ' + str(json_read(pending)['backup']))
            ffprobe = shutil.which('ffprobe')
            ffmpeg = shutil.which('ffmpeg')
            if not ffprobe or (args.apply and args.aac_action == 'convert' and not ffmpeg):
                raise RuntimeError('Installer/charger ffprobe et, pour convertir, ffmpeg avec libmp3lame')
            if args.apply and args.aac_action == 'convert':
                encoders = run_command([ffmpeg, '-hide_banner', '-encoders'], args.probe_timeout)
                if b'libmp3lame' not in encoders:
                    raise RuntimeError('Cet FFmpeg ne contient pas l’encodeur libmp3lame')
            files, links, metadata = collect(conn, tables, root)
            report_path = args.report.resolve()
            if report_path == db or report_path in files or within(report_path, root / '_state'):
                raise RuntimeError('Le rapport ne peut pas écraser la base, un média ou un reçu')
            url_paths = {}
            for path, urls in links.items():
                if path.is_file():
                    for url in urls:
                        url_paths.setdefault(url, set()).add(path)
            ambiguous = {url for url, paths in url_paths.items() if len(paths) > 1}
            backup = None
            run_id = uuid.uuid4().hex
            if args.apply:
                backup = args.backup_dir.resolve() if args.backup_dir else root.parent / (root.name + '_cleanup_' + time.strftime('%Y%m%d-%H%M%S') + '_' + run_id[:8])
                if within(backup, root) or backup.exists():
                    raise RuntimeError('--backup-dir doit être un nouveau dossier hors du corpus')
                backup.mkdir(parents=True)
                json_write(backup / 'run.json', dict(run_id=run_id, root=str(root), db=str(db), at=time.time()))
                json_write(pending, dict(backup=str(backup), run_id=run_id))
                with contextlib.closing(sqlite3.connect(str(backup / 'database.sqlite'))) as destination:
                    conn.backup(destination)
                ensure_write_schema(conn)
                print('Sauvegarde : ' + str(backup), flush=True)
            args.report.parent.mkdir(parents=True, exist_ok=True)
            counts, failures = {}, 0
            print(('APPLICATION' if args.apply else 'SIMULATION') + ' : ' + str(len(files)) + ' fichiers', flush=True)
            with args.report.open('w', newline='', encoding='utf-8-sig') as stream, ThreadPoolExecutor(max_workers=args.workers) as pool:
                writer = csv.writer(stream)
                writer.writerow(['file', 'kind', 'size_bytes', 'audio_urls', 'action', 'result', 'detail'])
                results = inspections(pool, files, ffprobe, args.probe_timeout, args.workers * 2)
                for index, entry in enumerate(results, 1):
                    kind = entry['kind']
                    counts[kind] = counts.get(kind, 0) + 1
                    urls = links.get(Path(entry['path']), set())
                    action = ('remove_image' if kind == 'image' else args.aac_action if kind == 'aac' else 'keep')
                    outcome, detail = 'planned' if action != 'keep' else 'unchanged', entry['detail']
                    if args.apply and action != 'keep':
                        try:
                            if urls & ambiguous:
                                raise RuntimeError('Une URL correspond à plusieurs fichiers locaux ; association ambiguë, conservée')
                            outcome = apply_item(entry, urls, metadata, conn, tables, root, backup, run_id, args, ffmpeg, ffprobe)
                        except Exception as error:
                            failures += 1
                            outcome, detail = 'error', str(error)
                            print('Échec (fichier conservé/récupérable) : ' + entry['path'] + ' : ' + detail, file=sys.stderr, flush=True)
                            # Une opération sans journal finalisé impose une récupération explicite.
                            if any(json_read(p)['status'] == 'prepared' for p in (backup / 'jobs').glob('*.json')):
                                raise RuntimeError('Récupération requise avec --recover ' + str(backup)) from error
                    writer.writerow([entry['path'], kind, entry['size_bytes'], json.dumps(sorted(urls), ensure_ascii=False), action, outcome, detail])
                    if index % 100 == 0 or index == len(files) or action != 'keep':
                        stream.flush()
                        print('{}/{} {}'.format(index, len(files), counts), flush=True)
            if args.apply:
                pending.unlink()
            print('Rapport : ' + str(args.report.resolve()))
            print('Résumé : ' + str(counts) + ' ; erreurs d’application : ' + str(failures))
            return 1 if failures else 0


def main():
    args = build_parser().parse_args()
    try:
        return run(args)
    except KeyboardInterrupt:
        print('\nInterruption ; si un nettoyage était appliqué, suivre --recover indiqué dans _state/cleanup_pending.json.', file=sys.stderr)
        return 130
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as error:
        print('Erreur : ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
