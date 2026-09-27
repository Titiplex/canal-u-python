import sys
import time
from datetime import datetime, timedelta


def duration(seconds):
    days, seconds = divmod(max(0, int(seconds)), 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    return f'{days}j {hours:02d}:{minutes:02d}:{seconds:02d}'


class LoadingBar:
    def __init__(self, total: int):
        self.total = max(0, total)
        self.point = 0
        self.start_time = time.monotonic()
        self._last_print = None
        self._width = 0

    def increment(self):
        self.point += 1

    def print(self, force=False, paused=False):
        now = time.monotonic()
        if not force and self._last_print is not None and now - self._last_print < 0.5:
            return
        self._last_print = now
        elapsed = max(0, now - self.start_time)
        pct = min(100, self.point * 100 / self.total) if self.total else 100
        filled = int(pct / 5)
        left = max(0, self.total - self.point)
        eta = 0 if left == 0 else (elapsed / self.point * left if self.point else None)
        if paused:
            remaining, finish = 'pause', '--'
        elif eta is None:
            remaining, finish = '--:--:--', '--'
        else:
            remaining = duration(eta)
            finish = (datetime.now() + timedelta(seconds=eta)).strftime('%d/%m %H:%M:%S')
        rate = self.point / elapsed if elapsed > 0 else 0
        text = (f'[{"=" * filled}{" " * (20 - filled)}] {pct:6.2f}% '
                f'{self.point}/{self.total} | Écoulé : {duration(elapsed)} '
                f'| Restant : {remaining} | Fin estimée : {finish} | {rate:.2f} page/s')
        sys.stdout.write('\r' + text.ljust(self._width))
        self._width = len(text)
        sys.stdout.flush()

    def elapsed(self):
        print('\nTotal de cette étape : ' + duration(time.monotonic() - self.start_time))
