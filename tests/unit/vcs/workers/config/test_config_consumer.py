from unittest.mock import Mock

import vcs.workers.config.config_consumer as config_consumer
from utils.helper import path_normalize
from vcs.shared.types import (
    ConfigDeletedEvent,
    ConfigModifiedEvent,
    ConfigMovedEvent,
    CreatedEvent,
    DeletedEvent,
    ModifiedEvent,
)
from vcs.workers.config.config_consumer import ConfigConsumer


def _consumer(**kwargs):
    kwargs.setdefault("db_handler", Mock())
    kwargs.setdefault("watcher", Mock())
    kwargs.setdefault("publish", Mock())
    return ConfigConsumer(**kwargs)


def _stub_config_io(monkeypatch, added=(), deleted=(), watch_targets=()):
    """Stub the config reads handle() makes.

    The `config=` kwargs are load-bearing, not incidental: handle() reads the
    config once and threads that object through diff/derive/snapshot so the
    baseline can't advance past unapplied changes. Stubs must accept it.
    """
    sentinel = {"sources": []}
    monkeypatch.setattr(config_consumer, "parse_config", lambda: sentinel)
    monkeypatch.setattr(
        config_consumer, "get_config_diff",
        lambda config=None: {"added": list(added), "deleted": list(deleted)}
    )
    monkeypatch.setattr(
        config_consumer, "derive_watch_targets",
        lambda config=None: list(watch_targets)
    )
    snapshot = Mock()
    monkeypatch.setattr(config_consumer, "store_config_snapshot", snapshot)
    return sentinel, snapshot


def test_handle_ignores_non_config_events(monkeypatch):
    diff_mock = Mock()
    monkeypatch.setattr(config_consumer, "get_config_diff", diff_mock)
    consumer = _consumer()

    assert consumer.handle(ModifiedEvent(src="a.txt")) is None
    diff_mock.assert_not_called()


def test_handle_config_deleted_recovers_config(monkeypatch):
    recover_mock = Mock()
    monkeypatch.setattr(config_consumer, "recover_config", recover_mock)

    _consumer().handle(ConfigDeletedEvent(src="cfg"))

    recover_mock.assert_called_once()


def test_handle_config_moved_recovers_config(monkeypatch):
    recover_mock = Mock()
    monkeypatch.setattr(config_consumer, "recover_config", recover_mock)

    _consumer().handle(ConfigMovedEvent(src="cfg", dst="cfg2"))

    recover_mock.assert_called_once()


def test_handle_config_deleted_does_not_apply_diff(monkeypatch):
    monkeypatch.setattr(config_consumer, "recover_config", Mock())
    diff_mock = Mock()
    monkeypatch.setattr(config_consumer, "get_config_diff", diff_mock)

    _consumer().handle(ConfigDeletedEvent(src="cfg"))

    diff_mock.assert_not_called()


def test_handle_config_modified_deactivates_deleted_sources(monkeypatch, tmp_path):
    old_source = tmp_path / "old_source"
    old_source.mkdir()
    old_file = old_source / "a.txt"
    old_file.write_text("hi")

    _stub_config_io(monkeypatch, deleted=[str(old_source)])
    deleted_mock = Mock()
    monkeypatch.setattr(config_consumer, "deleted_handle", deleted_mock)

    consumer = _consumer()
    consumer.handle(ConfigModifiedEvent(src="cfg"))

    deleted_mock.assert_called_once()
    db_handler, event = deleted_mock.call_args[0]
    assert db_handler is consumer.db_handler
    assert isinstance(event, DeletedEvent)
    assert event.src == path_normalize(str(old_file))


def test_handle_config_modified_inits_new_sources_in_db(monkeypatch, tmp_path):
    new_source = tmp_path / "new_source"
    new_source.mkdir()
    new_file = new_source / "a.txt"
    new_file.write_text("hi")

    _stub_config_io(
        monkeypatch, added=[str(new_source)], watch_targets=[str(new_source)]
    )
    created_mock = Mock()
    monkeypatch.setattr(config_consumer, "created_handle", created_mock)

    consumer = _consumer()
    consumer.handle(ConfigModifiedEvent(src="cfg"))

    created_mock.assert_called_once()
    db_handler, event = created_mock.call_args[0]
    assert db_handler is consumer.db_handler
    assert isinstance(event, CreatedEvent)
    assert event.src == path_normalize(str(new_file))


def test_handle_config_modified_reconciles_watches_from_derived_targets(monkeypatch):
    """Watches are reconciled against the derived set, not added per changed
    source: adding a file whose directory is already watched must not add a
    second watch."""
    _stub_config_io(monkeypatch, watch_targets=["/a", "/c"])

    consumer = _consumer()
    consumer.handle(ConfigModifiedEvent(src="cfg"))

    consumer.watcher.reconcile.assert_called_once_with(
        ["/a", "/c"], callback=consumer.publish
    )
    consumer.watcher.add_watch.assert_not_called()
    consumer.watcher.remove_watch.assert_not_called()


def test_handle_config_modified_snapshots_the_config_it_applied(monkeypatch):
    """Not a fresh read. config.yaml may have been rewritten while we worked;
    snapshotting the newer state would drop the unapplied change permanently."""
    config, snapshot_mock = _stub_config_io(monkeypatch)

    _consumer().handle(ConfigModifiedEvent(src="cfg"))

    snapshot_mock.assert_called_once_with(config_content=config)
