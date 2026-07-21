from pathlib import Path

from utils.helper import get_config_path
from vcs.services.configure import derive_watch_targets, is_path_in_scope, parse_config
from vcs.shared.types import CONFIG_EVENTS
from vcs.workers.local.local_queue import LocalQueue
from vcs.workers.local.local_watcher import WatchWorker
from vcs.workers.consumer_worker import ConsumerWorker
from vcs.workers.config.config_consumer import ConfigConsumerWorker
from vcs.workers.utils import STOP

class LocalRuntime:
    def __init__(self, sources, stop_event, watcher=None, queue=None, config_queue=None):
        self.stop_event = stop_event
        self._scope_cache = None

        if watcher:
            self.watcher = watcher
        else:
            self.watcher = WatchWorker(self.stop_event)

        self.consumer_worker = ConsumerWorker(
            self.stop_event,
            queue=queue
        )

        if queue:
            self.queue = queue
        else:
            self.queue = self.consumer_worker.queue

        self.config_queue = config_queue if config_queue else LocalQueue()

        self.config_consumer_worker = ConfigConsumerWorker(
            self.stop_event,
            watcher=self.watcher,
            publish=self._route,
            queue=self.config_queue
        )

        self._init_worker(sources)

    def _scope_config(self):
        """Parsed config, memoized.

        is_path_in_scope() re-reads and re-parses config.yaml on every call,
        which is fine per MCP tool call but not per filesystem event.

        No lock: watchdog's BaseObserver.dispatch_events runs every handler on
        a single dispatcher thread, so _route and _route_config_only are
        serialized against each other. If that ever stops holding, this cache
        needs one.
        """
        if self._scope_cache is None:
            self._scope_cache = parse_config()
        return self._scope_cache

    def _route(self, event):
        """Demux watcher events onto the queue owned by the worker that handles them."""
        if isinstance(event, CONFIG_EVENTS):
            self.config_queue.publish(event)
            return

        # Watch targets are directories derived from file-granular sources, so
        # a watch is deliberately broader than the granted scope. This filter
        # is what keeps unapproved siblings from being versioned - without it,
        # watching /a because /a/x.txt was approved would also track /a/secret.
        if not self._in_scope(event):
            return

        self.queue.publish(event)

    def _in_scope(self, event):
        config = self._scope_config()
        if is_path_in_scope(event.src, config=config):
            return True
        # A move out of scope still has to be recorded as a departure.
        dst = getattr(event, "dst", None)
        return dst is not None and is_path_in_scope(dst, config=config)

    def _route_config_only(self, event):
        """Router for the config-file watch.

        That watch covers the config file's whole parent directory, so it also
        sees unrelated siblings. Drop everything that is not a config event -
        those files are only in scope if a source watch covers them.
        """
        if isinstance(event, CONFIG_EVENTS):
            # Invalidate here rather than in ConfigConsumer so the new scope
            # applies to the very next event, without waiting for the config
            # worker to drain its queue.
            self._scope_cache = None
            self.config_queue.publish(event)

    def _init_worker(self, sources):
        # Derived rather than one watch per source, so startup and hot-add
        # produce identical watch sets. Derived from the injected `sources`,
        # not from disk, so construction stays testable and doesn't schedule
        # watchers against the real config behind a caller's back.
        self.watcher.reconcile(
            derive_watch_targets({"sources": sources}),
            callback=self._route,
            tag="source"
        )

        # watchdog is unreliable watching a single file and editors save via
        # atomic rename, so watch the parent. Non-recursive is mandatory: the
        # parent is usually the repo root.
        self.watcher.add_watch(
            str(Path(get_config_path()).parent),
            callback=self._route_config_only,
            recursive=False,
            tag="config",
            debounce=False
        )

    def run(self):
        self.watcher.start()
        self.consumer_worker.start()
        self.config_consumer_worker.start()

        try:
            while not self.stop_event.is_set():
                self.stop_event.wait(timeout=1.0)
        except KeyboardInterrupt:
            self.stop()


    def stop(self):
        self.stop_event.set()
        self.watcher.stop()
        self.queue.publish(STOP)
        self.config_queue.publish(STOP)
        self.consumer_worker.join()
        self.config_consumer_worker.join()
