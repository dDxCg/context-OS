from pathlib import Path

from utils.helper import get_config_path
from vcs.services.configure import derive_watch_targets, is_path_in_scope, parse_config
from vcs.shared.types import CONFIG_EVENTS
from vcs.workers.bus import LocalEventBus
from vcs.workers.local.local_watcher import WatchWorker
from vcs.workers.consumer_worker import ConsumerWorker
from vcs.workers.config.config_consumer import ConfigConsumerWorker

SOURCE_BINDING = "source.#"
CONFIG_BINDING = "config.#"


class LocalRuntime:
    def __init__(self, sources, stop_event, watcher=None, bus=None):
        self.stop_event = stop_event
        self._scope_cache = None

        self.watcher = watcher if watcher else WatchWorker(self.stop_event)
        self.bus = bus if bus else LocalEventBus()

        # Bindings, not hand-written routing. The scope predicate belongs to the
        # source subscription, so config events are exempt by construction
        # rather than by an early return in a router.
        self.source_sub = self.bus.subscribe(SOURCE_BINDING, where=self._in_scope)
        self.config_sub = self.bus.subscribe(CONFIG_BINDING)

        self.consumer_worker = ConsumerWorker(
            self.stop_event,
            queue=self.source_sub.queue
        )

        self.config_consumer_worker = ConfigConsumerWorker(
            self.stop_event,
            watcher=self.watcher,
            publish=self._publish,
            queue=self.config_sub.queue
        )

        self._init_worker(sources)

    @property
    def queue(self):
        """Mailbox the source consumer reads. Kept as a name for readability."""
        return self.source_sub.queue

    @property
    def config_queue(self):
        return self.config_sub.queue

    def _scope_config(self):
        """Parsed config, memoized.

        is_path_in_scope() re-reads and re-parses config.yaml on every call,
        which is fine per MCP tool call but not per filesystem event.

        No lock: watchdog's BaseObserver.dispatch_events runs every handler on
        a single dispatcher thread, so every _publish - and therefore every
        evaluation of the scope predicate during fanout - is serialized. This
        is why the predicate is evaluated at fanout rather than in the consumer
        thread; moving it there would make this cache genuinely cross-thread
        and require a lock.
        """
        if self._scope_cache is None:
            self._scope_cache = parse_config()
        return self._scope_cache

    def _publish(self, event):
        """Publish onto the bus.

        The only logic here is cache coherence, not routing: a config change
        must invalidate the scope cache immediately, on this thread, so the
        very next source event is matched against the new scope instead of
        waiting for the config worker to drain its queue.
        """
        if isinstance(event, CONFIG_EVENTS):
            self._scope_cache = None
        self.bus.publish(event)

    def _in_scope(self, event):
        """Predicate for the source subscription.

        Watch targets are directories derived from file-granular sources, so a
        watch is deliberately broader than the granted scope. This is what
        keeps unapproved siblings from being versioned - without it, watching
        /a because /a/x.txt was approved would also track /a/secret. It is also
        what keeps unrelated files in the config file's parent directory out,
        now that the config watch publishes through the same bus.
        """
        config = self._scope_config()
        if is_path_in_scope(event.src, config=config):
            return True
        # A move out of scope still has to be recorded as a departure.
        dst = getattr(event, "dst", None)
        return dst is not None and is_path_in_scope(dst, config=config)

    def _init_worker(self, sources):
        # Derived rather than one watch per source, so startup and hot-add
        # produce identical watch sets. Derived from the injected `sources`,
        # not from disk, so construction stays testable and doesn't schedule
        # watchers against the real config behind a caller's back.
        self.watcher.reconcile(
            derive_watch_targets({"sources": sources}),
            callback=self._publish,
            tag="source"
        )

        # watchdog is unreliable watching a single file and editors save via
        # atomic rename, so watch the parent. Non-recursive is mandatory: the
        # parent is usually the repo root.
        self.watcher.add_watch(
            str(Path(get_config_path()).parent),
            callback=self._publish,
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
        # One call, however many subscribers exist. Hand-writing a STOP per
        # queue is what let the consumer set drift out of sync with shutdown
        # (issues.md #1).
        self.bus.close()
        self.consumer_worker.join()
        self.config_consumer_worker.join()
