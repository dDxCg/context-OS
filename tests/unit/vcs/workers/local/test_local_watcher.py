import threading

from utils.helper import path_normalize
from vcs.workers.local.local_watcher import WatchWorker


def test_add_watch_registers_a_job():
    worker = WatchWorker(threading.Event())

    worker.add_watch("some/path", callback=lambda event: None, recursive=False)

    assert len(worker.jobs) == 1
    assert worker.jobs[0][0] == path_normalize("some/path")


def test_remove_watch_unregisters_a_previously_added_path():
    worker = WatchWorker(threading.Event())
    worker.add_watch("some/path", callback=lambda event: None, recursive=False)

    worker.remove_watch("some/path")

    assert worker.jobs == []


def _mkdir(parent, name):
    d = parent / name
    d.mkdir()
    return d


def _noop(event):
    pass


def test_reconcile_adds_missing_and_removes_stale(tmp_path):
    a = _mkdir(tmp_path, "a")
    b = _mkdir(tmp_path, "b")
    c = _mkdir(tmp_path, "c")
    worker = WatchWorker(threading.Event())
    worker.reconcile([str(a), str(b)], callback=_noop)

    worker.reconcile([str(b), str(c)], callback=_noop)

    assert {j[0] for j in worker.jobs} == {b.as_posix(), c.as_posix()}


def test_reconcile_is_idempotent(tmp_path):
    a = _mkdir(tmp_path, "a")
    worker = WatchWorker(threading.Event())
    worker.reconcile([str(a)], callback=_noop)
    first = worker.jobs[0][2]

    worker.reconcile([str(a)], callback=_noop)

    assert len(worker.jobs) == 1
    assert worker.jobs[0][2] is first, "an unchanged watch was needlessly rescheduled"


def test_reconcile_leaves_other_tags_untouched(tmp_path):
    """The config-file watch is owned by LocalRuntime; reconciling source
    watches must never collect it."""
    cfg = _mkdir(tmp_path, "cfgdir")
    a = _mkdir(tmp_path, "a")
    worker = WatchWorker(threading.Event())
    worker.add_watch(str(cfg), callback=_noop, recursive=False, tag="config")

    worker.reconcile([str(a)], callback=_noop, tag="source")

    tags = {j[0]: j[3] for j in worker.jobs}
    assert tags == {cfg.as_posix(): "config", a.as_posix(): "source"}


def test_reconcile_to_empty_removes_all_tagged(tmp_path):
    a = _mkdir(tmp_path, "a")
    worker = WatchWorker(threading.Event())
    worker.reconcile([str(a)], callback=_noop)

    worker.reconcile([], callback=_noop)

    assert worker.jobs == []


def test_remove_watch_matches_regardless_of_path_format(tmp_path):
    """_init_worker registers raw config strings while get_config_diff() yields
    normalized posix paths. Without normalization on both sides, removing a
    source watched at startup silently no-ops and it keeps being versioned."""
    target = tmp_path / "src_a"
    target.mkdir()
    worker = WatchWorker(threading.Event())

    # Registered the way _init_worker does it - raw, backslashed on Windows.
    worker.add_watch(str(target), callback=lambda event: None, recursive=False)
    assert len(worker.jobs) == 1

    # Removed the way ConfigConsumer does it - normalized posix.
    worker.remove_watch(target.as_posix())

    assert worker.jobs == []
