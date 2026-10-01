"""Préparer/vérifier les lots, puis soumettre le tableau Slurm en une commande."""
import argparse
import json
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from transcribe import Lang, PROJECT, _parse_lang
from transcribe_slurm import _read_plan, _read_shard, prepare


def launch(plan_dir, db_path=None, dir_path=None, num_shards=None, max_parallel=4,
           lang=None, model_name=None, batch_path=None):
    if max_parallel < 1 or (num_shards is not None and num_shards < 1):
        raise ValueError('Nombre de lots et parallélisme >= 1 requis')
    batch = Path(batch_path).resolve() if batch_path else PROJECT / 'transcribe_array.sh'
    if not batch.is_file():
        raise FileNotFoundError('Script Slurm absent : ' + str(batch))
    options = re.findall(r'^\s*#SBATCH\s+--ntasks(?:=|\s+)(\d+)\s*$', batch.read_text(), re.MULTILINE)
    if not options or any(value != '1' for value in options):
        raise ValueError('Ce lancement requiert --ntasks=1 : chaque élément du tableau traite son propre lot')
    if shutil.which('sbatch') is None:
        raise RuntimeError('sbatch introuvable ; exécuter cette commande sur le cluster')
    directory = Path(plan_dir).resolve()
    if directory.exists():
        if not (directory / 'plan.json').is_file():
            raise ValueError('Dossier existant sans plan.json : ' + str(directory) +
                             '. Choisir un nouveau --plan (ne pas supprimer des résultats existants).')
        directory, meta = _read_plan(directory)
        checks = {
            'db_path': str(Path(db_path).resolve()) if db_path else None,
            'audio_root': str(Path(dir_path).resolve()) if dir_path else None,
            'num_shards': num_shards,
            'lang': (lang.value if isinstance(lang, Lang) else _parse_lang(lang).value) if lang is not None else None,
            'model': model_name,
        }
        for key, expected in checks.items():
            if expected is not None and expected != meta[key]:
                raise ValueError('Le plan existant a une autre valeur de ' + key +
                                 ' ; utiliser ses paramètres ou un nouveau dossier --plan')
        print('Réutilisation du plan existant : ' + str(directory), flush=True)
    else:
        meta = prepare(directory, db_path, dir_path, num_shards or 48,
                       lang or Lang.fr, model_name or 'turbo')
    # Vérifier tous les lots avant de réserver des GPU, y compris lors d'une reprise.
    for index in range(meta['num_shards']):
        _read_shard(directory, meta, index)
    (PROJECT / 'logs').mkdir(parents=True, exist_ok=True)
    command = ['sbatch', '--chdir=' + str(PROJECT),
               f"--array=0-{meta['num_shards'] - 1}%{max_parallel}", str(batch), str(directory)]
    print('Soumission : ' + json.dumps(command, ensure_ascii=False), flush=True)
    subprocess.run(command, check=True)
    return meta


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', default=str(PROJECT / 'transcription_batches'))
    parser.add_argument('--db', default=None)
    parser.add_argument('-d', '--dir', default=None)
    parser.add_argument('--num-shards', type=int, default=None, help='48 pour un nouveau plan ; sinon valeur du plan')
    parser.add_argument('--max-parallel', type=int, default=4)
    parser.add_argument('-l', '--lang', type=_parse_lang, default=None, choices=list(Lang))
    parser.add_argument('--model', default=None)
    parser.add_argument('--batch', default=None)
    args = parser.parse_args()
    try:
        launch(args.plan, args.db, args.dir, args.num_shards, args.max_parallel,
               args.lang, args.model, args.batch)
        return 0
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, subprocess.CalledProcessError) as error:
        print('Erreur : ' + str(error))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
