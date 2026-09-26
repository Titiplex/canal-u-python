"""Verrou OS libéré même après un crash, pour une seule instance par base locale."""
import os
from pathlib import Path


class DatabaseRunLock:
    def __init__(self, db_path):
        self.path = Path(str(Path(db_path).resolve()) + '.crawl.lock')
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        self.file.seek(0, 2)
        if self.file.tell() == 0:
            self.file.write(b'0')
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            self.file = None
            raise RuntimeError(f'Base déjà utilisée par un autre crawl : {self.path}') from error
        return self

    def __exit__(self, *args):
        # Fermer le descripteur libère le verrou. Ne pas supprimer le fichier :
        # une autre instance pourrait déjà avoir ouvert le même inode.
        self.file.close()
        self.file = None
