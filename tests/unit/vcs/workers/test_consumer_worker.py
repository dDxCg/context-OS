import threading

import pytest

import vcs.workers.consumer_worker as consumer_worker_module
from vcs.workers.consumer_worker import ConsumerWorker
from vcs.workers.interfaces.consumer import Consumer
from vcs.workers.local.local_queue import LocalQueue
from vcs.shared.types import ModifiedEvent
from vcs.workers.utils import STOP


@pytest.fixture(autouse=True)
def in_memory_db(monkeypatch):
    monkeypatch.setattr(consumer_worker_module, "get_db_url", lambda: ":memory:")


class _RecordingConsumer(Consumer):
    """Raises on the first event, records every one it sees."""

    seen = None

    def handle(self, event):
        type(self).seen.append(event)
        if getattr(event, "src", None) == "boom":
            raise RuntimeError("handler blew up")


def _run_worker(events):
    _RecordingConsumer.seen = []
    queue = LocalQueue()
    worker = ConsumerWorker(threading.Event(), consumer_cls=_RecordingConsumer, queue=queue)
    worker.daemon = True
    worker.start()
    for e in events:
        queue.publish(e)
    queue.publish(STOP)
    worker.join(timeout=5)
    return worker, _RecordingConsumer.seen


def test_handler_exception_does_not_kill_the_worker():
    """One bad event used to end the thread for the rest of the process
    lifetime - threading.excepthook swallows it and nothing recovers."""
    good_before = ModifiedEvent(src="before")
    bad = ModifiedEvent(src="boom")
    good_after = ModifiedEvent(src="after")

    worker, seen = _run_worker([good_before, bad, good_after])

    assert [getattr(e, "src", None) for e in seen] == ["before", "boom", "after"]
    assert not worker.is_alive(), "worker should have exited via STOP, not a crash"


def test_worker_still_stops_cleanly_after_a_handler_exception():
    worker, _ = _run_worker([ModifiedEvent(src="boom")])

    assert not worker.is_alive()


def test_worker_survives_a_failed_startup(monkeypatch):
    """A DB-connect failure used to kill the worker before its loop began,
    leaving a process that looks healthy but consumes nothing."""
    class _Exploding(Consumer):
        @classmethod
        def from_db_url(cls, db_url):
            raise RuntimeError("cannot connect")

        def handle(self, event):
            raise AssertionError("should never be reached")

    worker = ConsumerWorker(threading.Event(), consumer_cls=_Exploding, queue=LocalQueue())
    worker.daemon = True
    worker.start()
    worker.join(timeout=5)

    assert not worker.is_alive(), "failed startup should return, not hang"
