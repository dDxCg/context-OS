import threading

import pytest

import vcs.workers.config.config_consumer as config_consumer_module
import vcs.workers.consumer_worker as consumer_worker_module
from vcs.workers.local.local_runtime import LocalRuntime


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    monkeypatch.setattr(consumer_worker_module, "get_db_url", lambda: ":memory:")
    monkeypatch.setattr(config_consumer_module, "get_db_url", lambda: ":memory:")


@pytest.mark.xfail(strict=True, reason=(
    "docs/issues.md#1: only one of ConsumerWorker/ConfigConsumerWorker ever "
    "dequeues the single STOP sentinel from their shared queue, so the other "
    "blocks forever on queue.get() and LocalRuntime.stop() hangs on its "
    ".join()."
))
def test_local_runtime_stop_returns_promptly():
    stop_event = threading.Event()
    runtime = LocalRuntime(sources=[], stop_event=stop_event)

    # Daemonize the workers so that if this test's premise (a hang) actually
    # reproduces, the leaked thread can never block interpreter/pytest exit.
    runtime.consumer_worker.daemon = True
    runtime.config_consumer_worker.daemon = True

    runtime.watcher.start()
    runtime.consumer_worker.start()
    runtime.config_consumer_worker.start()

    # Run stop() on its own daemon thread with a bounded join so a real
    # deadlock fails this assertion instead of hanging the test suite.
    stopper = threading.Thread(target=runtime.stop, daemon=True)
    stopper.start()
    stopper.join(timeout=5)

    assert not stopper.is_alive(), (
        "LocalRuntime.stop() did not return within 5s - a worker thread deadlocked"
    )
