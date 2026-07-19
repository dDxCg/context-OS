import time
import logging

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from utils.formatter import normalize_event


class WatchWorker:
    def __init__(self, stop_event, debounce=0.5):
        self.observer = Observer()
        self.jobs = []
        self.debounce = debounce
        self.last_event = {}
        self.stop_event = stop_event

    def _should_process(self, path):
        now = time.time()
        last = self.last_event.get(path, 0)

        if now - last < self.debounce:
            return False

        self.last_event[path] = now
        return True

    def add_watch(self, path, callback, recursive=True):
        worker = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                normalized = normalize_event(event)
                if normalized is None:
                    return

                if not worker._should_process(normalized.src):
                    return

                try:
                    logging.info(f"[WATCH] {normalized.src}")
                    callback(normalized)

                except Exception as e:
                    logging.error(f"[WATCH ERROR] {e}")

        watch = self.observer.schedule(Handler(), path, recursive=recursive)
        self.jobs.append((path, callback, watch))

    def remove_watch(self, path):
        """Stops watching a specific path."""
        for job in self.jobs:
            if job[0] == path:
                self.jobs.remove(job)
                self.observer.unschedule(job[2])
                logging.info(f"[WATCH] Removed watch for {path}")
                return

    def start(self):
        logging.info("WatchWorker starting...")
        self.observer.start()

    def stop(self):
        logging.info("WatchWorker stopping...")
        self.observer.stop()
        self.observer.join()

    def run(self):
        self.start()
        while not self.stop_event.is_set():
            self.observer.join(1)
        

