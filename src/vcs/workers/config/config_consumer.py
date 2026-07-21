import logging
from typing import Type

from vcs.workers.consumer_worker import ConsumerWorker
from vcs.workers.interfaces.consumer import Consumer

from vcs.shared.types import (
    CONFIG_EVENTS,
    ConfigMovedEvent,
    ConfigCreatedEvent,
    ConfigDeletedEvent,
    ConfigModifiedEvent,
    SourceEvent,
    CreatedEvent,
    DeletedEvent,
)
from vcs.services.configure import (
    derive_watch_targets,
    get_config_diff,
    parse_config,
    recover_config,
    store_config_snapshot,
)
from vcs.services.versioning import created_handle, deleted_handle
from utils.logger import log_enabled
from utils.helper import collect_files
from vcs.workers.interfaces.event_broker import EventBroker
from vcs.workers.local.local_queue import LocalQueue


class ConfigConsumer(Consumer):
    def __init__(self, db_handler, watcher=None, publish=None):
        super().__init__(db_handler)
        self.watcher = watcher
        # Callback new source watches publish through - the runtime's router.
        self.publish = publish

    @log_enabled
    def handle(self, event: SourceEvent):
        if not isinstance(event, CONFIG_EVENTS):
            return

        if isinstance(event, (ConfigDeletedEvent, ConfigMovedEvent)):
            recover_config()
            return

        if isinstance(event, (ConfigModifiedEvent, ConfigCreatedEvent)):
            if self.watcher is None:
                logging.warning("ConfigConsumer has no watcher; skipping config change")
                return

            # Read the config exactly once and use that snapshot for the diff,
            # the watch set, and the new baseline. config.yaml can be rewritten
            # while we work (the MCP guardrail appends one path per approval);
            # re-reading it per step would let the baseline advance past changes
            # we never applied, dropping them permanently.
            config = parse_config()
            diff = get_config_diff(config=config)

            for path in diff["added"]:
                for f in collect_files(path):
                    created_handle(self.db_handler, CreatedEvent(src=f))

            for path in diff["deleted"]:
                for f in collect_files(path):
                    deleted_handle(self.db_handler, DeletedEvent(src=f))

            # Watch targets are derived from the whole source list, not from the
            # diff: adding one file can leave the watch set unchanged (its
            # directory is already watched), and removing one can leave a watch
            # in place (a sibling source still needs it). Reconcile once, after
            # the DB writes, so no event can race a row we just deactivated.
            self.watcher.reconcile(
                derive_watch_targets(config=config), callback=self.publish
            )

            store_config_snapshot(config_content=config)


class ConfigConsumerWorker(ConsumerWorker):
    def __init__(
            self,
            stop_event,
            watcher,
            publish=None,
            consumer_cls: Type[Consumer] = ConfigConsumer,
            event_broker_cls: Type[EventBroker] = LocalQueue,
            queue=None
        ):
            super().__init__(stop_event, consumer_cls, event_broker_cls, queue)
            self.watcher = watcher
            self.publish = publish

    def _configure_consumer(self):
        self.consumer.watcher = self.watcher
        self.consumer.publish = self.publish
