"""Workers réseau/parsing : aucun accès SQLite, ressources fermées par leur propriétaire."""
import logging
from queue import Queue
from threading import Thread

from modules import crawling


class WorkerPool:
    def __init__(self, count, fetch):
        self.tasks = Queue()
        self.results = Queue()
        self.fetch = fetch
        self.threads = [Thread(target=self._run, name=f'crawl-{i + 1}') for i in range(count)]

    def _run(self):
        try:
            while True:
                url = self.tasks.get()
                if url is None:
                    break
                try:
                    if crawling.stopped():
                        raise crawling.CrawlCancelled('Pause globale')
                    value = self.fetch(url)
                except BaseException as error:
                    if (isinstance(error, crawling.RetryLater) and error.blocked) or not isinstance(error, Exception):
                        crawling.stop()
                    self.results.put((url, None, error))
                else:
                    self.results.put((url, value, None))
        finally:
            try:
                crawling.close()
            except Exception:
                logging.exception('Fermeture des ressources du worker')

    def __enter__(self):
        for thread in self.threads:
            thread.start()
        return self

    def submit(self, url):
        self.tasks.put(url)

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            crawling.stop()
        for thread in self.threads:
            self.tasks.put(None)
        for thread in self.threads:
            thread.join()
