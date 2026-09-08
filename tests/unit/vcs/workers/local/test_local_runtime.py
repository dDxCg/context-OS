import threading

import pytest
import yaml

from vcs.shared.types import ConfigModifiedEvent, ModifiedEvent, MovedEvent
from vcs.workers.local.local_runtime import LocalRuntime


@pytest.fixture
def scoped_runtime(config_path, tmp_path):
    """Runtime whose only source is tmp_path/source.

    Scope isolation matters here: the source subscription filters events
    against is_path_in_scope(), so without an isolated CONFIG_PATH these tests
    would resolve against the real repo config.
    """
    source = tmp_path / "source"
    source.mkdir()
    config_path.write_text(
        yaml.safe_dump({"sources": [{"type": "local", "path": str(source)}]})
    )
    runtime = LocalRuntime(
        sources=[{"type": "local", "path": str(source)}],
        stop_event=threading.Event(),
    )
    return runtime, source


def test_consumer_and_config_consumer_workers_use_independent_queues(config_path):
    config_path.write_text(yaml.safe_dump({"sources": []}))
    runtime = LocalRuntime(sources=[], stop_event=threading.Event())

    assert runtime.consumer_worker.queue is not runtime.config_consumer_worker.queue


def test_config_event_reaches_only_the_config_subscription(scoped_runtime):
    runtime, _ = scoped_runtime
    event = ConfigModifiedEvent(src="cfg")

    runtime._publish(event)

    assert runtime.config_queue.consume() is event
    assert runtime.queue.queue.empty()


def test_in_scope_event_reaches_only_the_source_subscription(scoped_runtime):
    runtime, source = scoped_runtime
    event = ModifiedEvent(src=str(source / "a.txt"))

    runtime._publish(event)

    assert runtime.queue.consume() is event
    assert runtime.config_queue.queue.empty()


def test_out_of_scope_event_is_dropped_by_the_subscription_predicate(scoped_runtime, tmp_path):
    """Watch targets are directories derived from file-granular sources, so a
    watch is broader than the grant. The predicate is what stops an unapproved
    sibling under a watched directory from being versioned."""
    runtime, _ = scoped_runtime
    event = ModifiedEvent(src=str(tmp_path / "outside.txt"))

    runtime._publish(event)

    assert runtime.queue.queue.empty()
    assert runtime.config_queue.queue.empty()


def test_unrelated_file_beside_the_config_file_is_not_published(scoped_runtime, config_path):
    """The config watch covers the config file's whole parent directory and now
    publishes through the same bus as source watches. What used to be
    _route_config_only's job is done by the scope predicate: a sibling of
    config.yaml belongs to no source, so it reaches neither subscription."""
    runtime, _ = scoped_runtime
    sibling = config_path.parent / "unrelated.txt"

    runtime._publish(ModifiedEvent(src=str(sibling)))

    assert runtime.queue.queue.empty()
    assert runtime.config_queue.queue.empty()


def test_move_admitted_when_only_dst_in_scope(scoped_runtime, tmp_path):
    runtime, source = scoped_runtime
    event = MovedEvent(src=str(tmp_path / "outside.txt"), dst=str(source / "a.txt"))

    runtime._publish(event)

    assert runtime.queue.consume() is event


def test_move_admitted_when_only_src_in_scope(scoped_runtime, tmp_path):
    """A file moved out of scope still has to be recorded as a departure."""
    runtime, source = scoped_runtime
    event = MovedEvent(src=str(source / "a.txt"), dst=str(tmp_path / "outside.txt"))

    runtime._publish(event)

    assert runtime.queue.consume() is event


def test_publishing_a_config_event_invalidates_the_scope_cache(scoped_runtime):
    """Invalidation happens on the publishing thread so the very next source
    event is matched against the new scope, not after the config worker
    drains its queue."""
    runtime, _ = scoped_runtime
    runtime._scope_config()
    assert runtime._scope_cache is not None

    runtime._publish(ConfigModifiedEvent(src="cfg"))

    assert runtime._scope_cache is None


def test_stop_broadcasts_to_every_subscription(config_path):
    """One bus.close(), however many subscribers exist - shutdown can no longer
    drift out of sync with the consumer set (issues.md #1)."""
    from vcs.workers.utils import STOP

    config_path.write_text(yaml.safe_dump({"sources": []}))
    runtime = LocalRuntime(sources=[], stop_event=threading.Event())

    runtime.bus.close()

    assert runtime.queue.consume() is STOP
    assert runtime.config_queue.consume() is STOP


def test_scope_cache_is_memoized(scoped_runtime, monkeypatch):
    """is_path_in_scope re-parses config.yaml per call; at per-event frequency
    that has to be cached."""
    runtime, _ = scoped_runtime
    import vcs.workers.local.local_runtime as module

    calls = []
    real = module.parse_config
    monkeypatch.setattr(module, "parse_config", lambda *a, **k: (calls.append(1), real())[1])

    runtime._scope_config()
    runtime._scope_config()
    runtime._scope_config()

    assert len(calls) == 1


def test_init_worker_collapses_file_sources_to_one_directory_watch(config_path, tmp_path):
    """Two approved files in one directory must produce a single watch - this
    is the whole point of deriving watch targets."""
    source = tmp_path / "docs"
    source.mkdir()
    (source / "a.txt").write_text("a")
    (source / "b.txt").write_text("b")
    config_path.write_text(yaml.safe_dump({"sources": []}))

    runtime = LocalRuntime(
        sources=[
            {"type": "local", "path": str(source / "a.txt")},
            {"type": "local", "path": str(source / "b.txt")},
        ],
        stop_event=threading.Event(),
    )

    source_watches = [j[0] for j in runtime.watcher.jobs if j[3] == "source"]
    assert source_watches == [source.as_posix()]
