import logging
import signal
import threading

from vcs.workers.local.local_runtime import LocalRuntime
from vcs.initialize import Initializer
from utils.logger import setup_logger

class VCSRuntime:
    def __init__(self):
        self.stop_event = threading.Event()
        self.initializer = Initializer()
        self.local_runtime = LocalRuntime(self.initializer.sources, self.stop_event)
        self._stopped = False

    def run(self):
        self.initializer.init()
        signal.signal(signal.SIGTERM, self._handle_signal)

        try:
            self.local_runtime.run()
        except KeyboardInterrupt:
            logging.info("Stopping VCS Runtime...")
        self.stop()

    def _handle_signal(self, signum, frame):
        logging.info("Received signal %s, stopping VCS Runtime...", signum)
        self.stop_event.set()

    def stop(self):
        if self._stopped:
            return
        self._stopped = True
        self.stop_event.set()
        self.local_runtime.stop()


if __name__ == "__main__":
    setup_logger(level=logging.DEBUG)
    vcs_runtime = VCSRuntime()
    vcs_runtime.run()