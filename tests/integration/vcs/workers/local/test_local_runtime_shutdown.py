import threading

import pytest

import vcs.workers.consumer_worker as consumer_worker_module
from vcs.workers.local.local_queue import LocalQueue
from vcs.workers.local.local_runtime import LocalRuntime
from vcs.workers.utils import STOP


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    # Both workers now inherit ConsumerWorker.run(), so this is the single
    # place get_db_url is resolved.
    monkeypatch.setattr(consumer_worker_module, "get_db_url", lambda: ":memory:")


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
    assert not runtime.consumer_worker.is_alive()
    assert not runtime.config_consumer_worker.is_alive()


def test_ac2_run_exits_when_stop_sentinel_file_appears(tmp_path):
    """spec 033: `ctx daemon stop`'s console-less fallback (Windows,
    git-bash/mintty) writes a sentinel file instead of a signal - run()'s
    main loop must notice it on its existing ~1s poll cadence."""
    stop_event = threading.Event()
    runtime = LocalRuntime(sources=[], stop_event=stop_event)
    runtime._stop_sentinel_path = tmp_path / "ctx.stop"
    runtime.consumer_worker.daemon = True
    runtime.config_consumer_worker.daemon = True

    # run() itself calls watcher/worker .start() - written before run() so
    # the very first loop iteration already sees it, no sleep needed.
    runtime._stop_sentinel_path.write_text("")

    runner = threading.Thread(target=runtime.run, daemon=True)
    runner.start()
    runner.join(timeout=5)

    assert not runner.is_alive(), "run() did not notice the sentinel file within 5s"
    assert stop_event.is_set()

    runtime.stop()


def test_queue_close_is_idempotent():
    """VCSRuntime.stop() closes the queue the consumer worker already closed on
    STOP. close() must tolerate that - it used to call task_done() and raise
    ValueError('task_done() called too many times') on the second call."""
    queue = LocalQueue()
    queue.publish(STOP)
    assert queue.consume() is STOP

    queue.close()
    queue.close()  # must not raise
