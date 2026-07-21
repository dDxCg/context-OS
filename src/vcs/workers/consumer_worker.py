import logging
import threading
from typing import Type

from utils.helper import get_db_url
from vcs.workers.interfaces.consumer import Consumer
from vcs.workers.interfaces.event_broker import EventBroker
from vcs.workers.local.local_consumer import LocalConsumer
from vcs.workers.local.local_queue import LocalQueue
from vcs.workers.utils import STOP


class ConsumerWorker(threading.Thread):
    def __init__(
            self, 
            stop_event, 
            consumer_cls: Type[Consumer] = LocalConsumer, 
            event_broker_cls: Type[EventBroker] = LocalQueue, 
            queue=None
        ):
            super().__init__()
            if queue:
                self.queue = queue
            else:
                self.queue = event_broker_cls()
            self.stop_event = stop_event
            self.consumer_cls = consumer_cls

    def _configure_consumer(self):
        """Hook for subclasses to inject collaborators into the consumer.

        The consumer is built inside run(), on the worker thread, so anything it
        depends on has to be attached there rather than in __init__.
        """
        pass

    def run(self):
        try:
            self.consumer = self.consumer_cls.from_db_url(get_db_url())
            self._configure_consumer()
        except Exception:
            # Without this the worker dies before its loop starts and the
            # failure is swallowed by threading.excepthook - the process looks
            # healthy while silently consuming nothing.
            logging.exception("[WORKER] failed to start; consuming nothing")
            return

        try:
            while True:
                event = self.queue.consume()

                if event is STOP:
                    break

                try:
                    self.consumer.handle(event)
                except Exception:
                    # One bad event must not take the worker down for the rest
                    # of the process lifetime. @log_enabled already logged and
                    # re-raised; this second record is the one saying we
                    # survived and kept consuming.
                    logging.exception("[WORKER] handler failed; continuing")
        finally:
            self.queue.close()