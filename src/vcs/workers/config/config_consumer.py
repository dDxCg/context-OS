import logging
from typing import Type

from vcs.workers.consumer_worker import ConsumerWorker
from vcs.workers.interfaces.consumer import Consumer

from vcs.shared.types import ConfigMovedEvent, ConfigCreatedEvent, ConfigDeletedEvent, ConfigModifiedEvent, SourceEvent, CreatedEvent
from vcs.services.configure import recover_config, store_config_snapshot, get_config_diff
from utils.logger import log_enabled
from utils.helper import collect_files, get_db_url
from vcs.workers.interfaces.event_broker import EventBroker
from vcs.workers.local.local_consumer import LocalConsumer
from vcs.workers.local.local_queue import LocalQueue
from vcs.workers.utils import STOP

class ConfigConsumer(Consumer):
    @log_enabled
    def handle(self, event: SourceEvent, runtime):
        from vcs.workers.local.local_runtime import LocalRuntime
        if not isinstance(runtime, LocalRuntime):
            logging.warning("ConfigConsumer not able to handle. Expected LocalRuntime in func handle")
            return
        if isinstance(event, (ConfigDeletedEvent, ConfigMovedEvent)):
            recover_config()
        if isinstance(event, ConfigModifiedEvent):
            diff = get_config_diff()
            added_paths = diff["added"]
            deleted_paths = diff["deleted"]
            for path in added_paths:
                files = collect_files(path)
                for f in files:
                    created_event = CreatedEvent(src=f)
                    runtime.consumer_worker.queue.publish(created_event)

                runtime.watcher.add_watch(
                    path=path,
                    callback=runtime.consumer_worker.consumer.handle
                )
            for path in deleted_paths:
                runtime.watcher.remove_watch(path)
            store_config_snapshot()
        
        return


class ConfigConsumerWorker(ConsumerWorker):
    def __init__(
            self, 
            stop_event,
            runtime, 
            consumer_cls: Type[Consumer] = ConfigConsumer, 
            event_broker_cls: Type[EventBroker] = LocalQueue, 
            queue=None
        ):
            super().__init__(stop_event, consumer_cls, event_broker_cls, queue)
            self.runtime = runtime

    @log_enabled
    def run(self):
        self.consumer = self.consumer_cls.from_db_url(get_db_url())
        while True:
            event = self.queue.consume()

            if event is STOP:
                self.queue.close()
                break

            self.consumer.handle(event, self.runtime)