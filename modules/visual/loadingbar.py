import sys
import time


class LoadingBar:
    def __init__(self, total: int):
        self.total = max(0, total)
        self.point = 0
        self.start_time = time.monotonic()

    def increment(self):
        self.point += 1

    def print(self):
        pct = min(100, self.point * 100 / self.total) if self.total else 100
        filled = int(pct / 5)
        if self.point:
            eta = int((time.monotonic() - self.start_time) / self.point * max(0, self.total - self.point))
            days, eta = divmod(eta, 86400)
            hours, eta = divmod(eta, 3600)
            minutes, seconds = divmod(eta, 60)
            remaining = f'{days}j {hours:02d}:{minutes:02d}:{seconds:02d}'
        else:
            remaining = '00:00:00' if not self.total else '--:--:--'
        sys.stdout.write(f'\r[{"=" * filled}{" " * (20 - filled)}] {pct:6.2f}% ETA : {remaining}     ')
        sys.stdout.flush()

    def elapsed(self):
        eta = int(time.monotonic() - self.start_time)
        days, eta = divmod(eta, 86400)
        hours, eta = divmod(eta, 3600)
        minutes, seconds = divmod(eta, 60)
        remaining = f'{days}j {hours:02d}:{minutes:02d}:{seconds:02d}'
        print("Total : " + remaining)
