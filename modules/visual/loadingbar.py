import sys
import time


class LoadingBar:
    def __new__(cls, *args, **kwargs):
        return super().__new__(cls)

    def __init__(self, total: int):
        self.total: int = total
        self.point: int = 0
        self.start_time = time.time()

    def increment(self) -> None:
        self.point += 1

    def print(self) -> None:
        string = "\r["

        pct = self.point * 100 / self.total
        status = int(int(pct) / 5)

        for i in range(status):
            string += "="
        for i in range(20 - status):
            string += " "

        string += "]"
        string += str(round(pct, 2))
        string += "%"

        # compute remaining time with cross product
        eta = int(((time.time() - self.start_time)/(self.point if self.point > 0 else 1))*(self.total - self.point))

        time_str = ""

        if eta > 60*60*24:
            time_str += str(int(eta/60*60*24)) + "D"
            eta = eta % 60*60*24
        if eta > 60*60:
            time_str += str(int(eta/60*60)) + "H"
            eta = eta % 60*60
        if eta > 60:
            time_str += str(int(eta/60)) + "M"
            eta = eta % 60
        time_str += str(eta) + "s"

        string += " ETA : " + time_str

        sys.stdout.write(string)
        sys.stdout.flush()
