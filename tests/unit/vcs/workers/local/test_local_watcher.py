import threading

from vcs.workers.local.local_watcher import WatchWorker


def test_add_watch_registers_a_job():
    worker = WatchWorker(threading.Event())

    worker.add_watch("some/path", callback=lambda event: None, recursive=False)

    assert len(worker.jobs) == 1
    assert worker.jobs[0][0] == "some/path"


def test_remove_watch_unregisters_a_previously_added_path():
    worker = WatchWorker(threading.Event())
    worker.add_watch("some/path", callback=lambda event: None, recursive=False)

    worker.remove_watch("some/path")

    assert worker.jobs == []
