from watchdog.events import (
    DirDeletedEvent,
    DirMovedEvent,
    FileCreatedEvent,
    FileDeletedEvent,
    FileModifiedEvent,
    FileMovedEvent,
)

from utils.formatter import normalize_event
from utils.helper import path_normalize
from vcs.shared.types import (
    ConfigCreatedEvent,
    CreatedEvent,
    DeletedEvent,
    ModifiedEvent,
    MovedEvent,
)


class _NoSrcPathEvent:
    src_path = ""


def test_normalize_event_returns_none_when_no_src_path(config_path):
    assert normalize_event(_NoSrcPathEvent()) is None


def test_normalize_event_maps_file_created(config_path, tmp_path):
    target = tmp_path / "a.txt"

    event = normalize_event(FileCreatedEvent(str(target)))

    assert isinstance(event, CreatedEvent)
    assert event.src == path_normalize(str(target))


def test_normalize_event_maps_file_modified(config_path, tmp_path):
    target = tmp_path / "a.txt"

    event = normalize_event(FileModifiedEvent(str(target)))

    assert isinstance(event, ModifiedEvent)
    assert event.src == path_normalize(str(target))


def test_normalize_event_maps_file_deleted(config_path, tmp_path):
    target = tmp_path / "a.txt"

    event = normalize_event(FileDeletedEvent(str(target)))

    assert isinstance(event, DeletedEvent)
    assert event.src == path_normalize(str(target))


def test_normalize_event_maps_file_moved(config_path, tmp_path):
    src = tmp_path / "a.txt"
    dst = tmp_path / "b.txt"

    event = normalize_event(FileMovedEvent(str(src), str(dst)))

    assert isinstance(event, MovedEvent)
    assert event.src == path_normalize(str(src))
    assert event.dst == path_normalize(str(dst))


def test_normalize_event_maps_dir_deleted(config_path, tmp_path):
    event = normalize_event(DirDeletedEvent(str(tmp_path)))

    assert isinstance(event, DeletedEvent)
    assert event.is_dir is True


def test_normalize_event_maps_dir_moved(config_path, tmp_path):
    src = tmp_path / "dir_a"
    dst = tmp_path / "dir_b"

    event = normalize_event(DirMovedEvent(str(src), str(dst)))

    assert isinstance(event, MovedEvent)
    assert event.is_dir is True


def test_normalize_event_maps_config_file_created(config_path):
    event = normalize_event(FileCreatedEvent(str(config_path)))

    assert isinstance(event, ConfigCreatedEvent)
