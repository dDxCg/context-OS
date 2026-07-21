import time
import logging

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from utils.formatter import normalize_event
from utils.helper import path_normalize


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

    def add_watch(self, path, callback, recursive=True, tag="source", debounce=True):
        """Watch `path`.

        `debounce=False` disables per-path event coalescing for this watch.
        Required for the config file: debouncing drops the *second* of two
        rapid writes, and for config that loss is permanent - the diff it
        carried is never re-applied, leaving config.yaml permanently out of
        sync with the DB and the watch set. Rapid successive writes are the
        normal case there, since the MCP guardrail calls add_sources() once
        per approved path. For ordinary files a dropped event is harmless;
        the next edit re-fires it.
        """
        # Normalize on the way in so remove_watch() can match by equality.
        # Callers disagree on format: _init_worker passes raw yaml strings
        # (possibly backslashed), while get_config_diff() yields posix paths.
        path = path_normalize(path)
        worker = self

        class Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                normalized = normalize_event(event)
                if normalized is None:
                    return

                if debounce and not worker._should_process(normalized.src):
                    return

                try:
                    logging.info(f"[WATCH] {normalized.src}")
                    callback(normalized)

                except Exception as e:
                    logging.error(f"[WATCH ERROR] {e}")

        watch = self.observer.schedule(Handler(), path, recursive=recursive)
        self.jobs.append((path, callback, watch, tag))

    def reconcile(self, desired, callback, recursive=True, tag="source"):
        """Make the `tag`-owned watch set exactly `desired`.

        Watches are derived from config sources rather than mapped 1:1 to them
        (see configure.derive_watch_targets), so a config change can collapse
        two watches into one or split one into two. Expressing that as
        add/remove deltas per changed source doesn't work - reconciling the
        whole tagged set does, and is idempotent.

        Only jobs carrying `tag` are considered, so reconciling source watches
        never collects the config-file watch.
        """
        desired = {path_normalize(p) for p in desired}
        current = {job[0] for job in self.jobs if job[3] == tag}

        for path in current - desired:
            self.remove_watch(path)

        for path in desired - current:
            self.add_watch(path, callback=callback, recursive=recursive, tag=tag)

    def remove_watch(self, path):
        """Stops watching a specific path."""
        path = path_normalize(path)
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
        

