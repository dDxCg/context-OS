import threading

import pytest
import yaml

from vcs.shared.types import ConfigModifiedEvent, ModifiedEvent, MovedEvent
from vcs.workers.local.local_runtime import LocalRuntime


@pytest.fixture
def scoped_runtime(config_path, tmp_path):
    """Runtime whose only source is tmp_path/source.

    Scope isolation matters here: _route now filters events against
    is_path_in_scope(), so without an isolated CONFIG_PATH these tests would
    resolve against the real repo config.
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


def test_route_sends_config_events_to_config_queue(scoped_runtime):
    runtime, _ = scoped_runtime
    event = ConfigModifiedEvent(src="cfg")

    runtime._route(event)

    assert runtime.config_queue.consume() is event
    assert runtime.queue.queue.empty()


def test_route_sends_in_scope_events_to_local_queue(scoped_runtime):
    runtime, source = scoped_runtime
    event = ModifiedEvent(src=str(source / "a.txt"))

    runtime._route(event)

    assert runtime.queue.consume() is event
    assert runtime.config_queue.queue.empty()


def test_route_drops_out_of_scope_events(scoped_runtime, tmp_path):
    """Watch targets are directories derived from file-granular sources, so a
    watch is broader than the grant. This filter is what stops an unapproved
    sibling under a watched directory from being versioned."""
    runtime, _ = scoped_runtime
    event = ModifiedEvent(src=str(tmp_path / "outside.txt"))

    runtime._route(event)

    assert runtime.queue.queue.empty()
    assert runtime.config_queue.queue.empty()


def test_route_admits_move_when_only_dst_in_scope(scoped_runtime, tmp_path):
    runtime, source = scoped_runtime
    event = MovedEvent(src=str(tmp_path / "outside.txt"), dst=str(source / "a.txt"))

    runtime._route(event)

    assert runtime.queue.consume() is event


def test_route_admits_move_when_only_src_in_scope(scoped_runtime, tmp_path):
    """A file moved out of scope still has to be recorded as a departure."""
    runtime, source = scoped_runtime
    event = MovedEvent(src=str(source / "a.txt"), dst=str(tmp_path / "outside.txt"))

    runtime._route(event)

    assert runtime.queue.consume() is event


def test_route_config_only_drops_plain_events(scoped_runtime):
    runtime, source = scoped_runtime

    runtime._route_config_only(ModifiedEvent(src=str(source / "a.txt")))

    assert runtime.queue.queue.empty()
    assert runtime.config_queue.queue.empty()


def test_route_config_only_invalidates_scope_cache(scoped_runtime):
    runtime, _ = scoped_runtime
    runtime._scope_config()
    assert runtime._scope_cache is not None

    runtime._route_config_only(ConfigModifiedEvent(src="cfg"))

    assert runtime._scope_cache is None


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
