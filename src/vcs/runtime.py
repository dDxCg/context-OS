import logging
import threading

from vcs.workers.local.local_runtime import LocalRuntime
from vcs.workers.local.local_queue import LocalQueue
from vcs.initialize import Initializer
from utils.logger import setup_logger

class VCSRuntime:
    def __init__(self):
        self.stop_event = threading.Event()
        self.initializer = Initializer()
        self.queue = LocalQueue()
        self.local_runtime = LocalRuntime(self.initializer.sources, self.stop_event, queue=self.queue)

    def run(self):
        self.initializer.init()

        try:
            self.local_runtime.run()
        except KeyboardInterrupt:
            logging.info("Stopping VCS Runtime...")
            self.stop()

    def stop(self):
        self.stop_event.set()
        self.local_runtime.stop()
        self.queue.close()


if __name__ == "__main__":
    setup_logger(level=logging.DEBUG)
    vcs_runtime = VCSRuntime()
    vcs_runtime.run()