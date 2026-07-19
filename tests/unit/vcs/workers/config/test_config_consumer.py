from unittest.mock import Mock

import vcs.workers.config.config_consumer as config_consumer
from utils.helper import path_normalize
from vcs.shared.types import ConfigDeletedEvent, ConfigModifiedEvent, ConfigMovedEvent, CreatedEvent
from vcs.workers.config.config_consumer import ConfigConsumer
from vcs.workers.local.local_runtime import LocalRuntime


def _fake_local_runtime():
    """A LocalRuntime instance that bypasses __init__ (no real threads/watcher
    spun up) but still satisfies ConfigConsumer.handle's isinstance check."""
    runtime = object.__new__(LocalRuntime)
    runtime.watcher = Mock()
    runtime.consumer_worker = Mock()
    runtime.consumer_worker.queue = Mock()
    runtime.consumer_worker.consumer = Mock()
    return runtime


def test_handle_ignores_non_local_runtime():
    consumer = ConfigConsumer(db_handler=None)

    result = consumer.handle(ConfigModifiedEvent(src="cfg"), runtime=object())

    assert result is None


def test_handle_config_deleted_recovers_config(monkeypatch):
    recover_mock = Mock()
    monkeypatch.setattr(config_consumer, "recover_config", recover_mock)
    runtime = _fake_local_runtime()

    ConfigConsumer(db_handler=None).handle(ConfigDeletedEvent(src="cfg"), runtime)

    recover_mock.assert_called_once()


def test_handle_config_moved_recovers_config(monkeypatch):
    recover_mock = Mock()
    monkeypatch.setattr(config_consumer, "recover_config", recover_mock)
    runtime = _fake_local_runtime()

    ConfigConsumer(db_handler=None).handle(ConfigMovedEvent(src="cfg", dst="cfg2"), runtime)

    recover_mock.assert_called_once()


def test_handle_config_modified_removes_watch_for_deleted_sources(monkeypatch):
    monkeypatch.setattr(
        config_consumer, "get_config_diff", lambda: {"added": [], "deleted": ["/old/source"]}
    )
    monkeypatch.setattr(config_consumer, "store_config_snapshot", Mock())
    runtime = _fake_local_runtime()

    ConfigConsumer(db_handler=None).handle(ConfigModifiedEvent(src="cfg"), runtime)

    runtime.watcher.remove_watch.assert_called_once_with("/old/source")


def test_handle_config_modified_publishes_created_event_for_new_files(monkeypatch, tmp_path):
    new_source = tmp_path / "new_source"
    new_source.mkdir()
    new_file = new_source / "a.txt"
    new_file.write_text("hi")

    monkeypatch.setattr(
        config_consumer, "get_config_diff", lambda: {"added": [str(new_source)], "deleted": []}
    )
    monkeypatch.setattr(config_consumer, "store_config_snapshot", Mock())
    runtime = _fake_local_runtime()

    ConfigConsumer(db_handler=None).handle(ConfigModifiedEvent(src="cfg"), runtime)

    runtime.consumer_worker.queue.publish.assert_called_once()
    published = runtime.consumer_worker.queue.publish.call_args[0][0]
    assert isinstance(published, CreatedEvent)
    assert published.src == path_normalize(str(new_file))
    runtime.watcher.add_watch.assert_called_once_with(
        path=str(new_source), callback=runtime.consumer_worker.consumer.handle
    )
