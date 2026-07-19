from unittest.mock import Mock

import pytest

import vcs.workers.local.local_consumer as local_consumer
from vcs.shared.temp_file import TempFile
from vcs.shared.types import CreatedEvent, DeletedEvent, ModifiedEvent, MovedEvent
from vcs.workers.local.local_consumer import LocalConsumer


@pytest.fixture(autouse=True)
def isolate_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(TempFile, "TMP_DIR", tmp_path / "tmp")


def test_handle_dispatches_moved_event(monkeypatch):
    mock = Mock()
    monkeypatch.setattr(local_consumer, "moved_handle", mock)
    consumer = LocalConsumer(db_handler="db")
    event = MovedEvent(src="a", dst="b")

    consumer.handle(event)

    mock.assert_called_once_with("db", event)


def test_handle_dispatches_deleted_event(monkeypatch):
    mock = Mock()
    monkeypatch.setattr(local_consumer, "deleted_handle", mock)
    consumer = LocalConsumer(db_handler="db")
    event = DeletedEvent(src="a")

    consumer.handle(event)

    mock.assert_called_once_with("db", event)


def test_handle_dispatches_created_event(monkeypatch):
    mock = Mock()
    monkeypatch.setattr(local_consumer, "created_handle", mock)
    consumer = LocalConsumer(db_handler="db")
    event = CreatedEvent(src="a")

    consumer.handle(event)

    mock.assert_called_once_with("db", event)


def test_handle_dispatches_modified_event_with_a_tmp_file_snapshot(monkeypatch, tmp_path):
    modified_mock = Mock()
    monkeypatch.setattr(local_consumer, "modified_handle", modified_mock)
    target = tmp_path / "a.txt"
    target.write_text("content")
    consumer = LocalConsumer(db_handler="db")
    event = ModifiedEvent(src=str(target))

    consumer.handle(event)

    assert modified_mock.call_count == 1
    args, kwargs = modified_mock.call_args
    assert args == ("db", event)
    assert kwargs["tmp_file"].read_bytes() == b"content"
