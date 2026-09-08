from unittest.mock import Mock

import pytest

import vcs.services.mirror_path as mirror_path
import vcs.workers.local.local_consumer as local_consumer
from vcs.services import git_store
from vcs.services.actor_hints import set_hint
from vcs.shared.temp_file import TempFile
from vcs.shared.types import (
    ConfigCreatedEvent,
    ConfigDeletedEvent,
    ConfigModifiedEvent,
    ConfigMovedEvent,
    CreatedEvent,
    DeletedEvent,
    ModifiedEvent,
    MovedEvent,
)
from vcs.workers.local.local_consumer import LocalConsumer


@pytest.fixture(autouse=True)
def isolate_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(TempFile, "TMP_DIR", tmp_path / "tmp")


@pytest.fixture(autouse=True)
def no_pending_hint_by_default(monkeypatch):
    """These tests dispatch with a fake db_handler ("db") and mocked
    handlers - they don't care about actor hints. AC-5/EC-2 below use a
    real db_handler and exercise consume_actor_hint for real."""
    monkeypatch.setattr(local_consumer, "consume_actor_hint", Mock(return_value=None))


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


@pytest.mark.parametrize("event", [
    ConfigCreatedEvent(),
    ConfigModifiedEvent(),
    ConfigDeletedEvent(),
    ConfigMovedEvent(dst="cfg2"),
])
def test_handle_ignores_config_events(monkeypatch, event):
    """Config events subclass the plain events, so without an explicit guard
    the isinstance chain would version the config file as ordinary content."""
    mocks = {}
    for name in ("moved_handle", "modified_handle", "deleted_handle", "created_handle"):
        mocks[name] = Mock()
        monkeypatch.setattr(local_consumer, name, mocks[name])

    LocalConsumer(db_handler="db").handle(event)

    for name, mock in mocks.items():
        mock.assert_not_called()


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


@pytest.fixture
def isolate_git_repo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror_path, "GIT_REPO_DIR", tmp_path / "git-repos")


def test_ac5_handle_fills_actor_from_pending_hint_before_dispatch(
    monkeypatch, db_handler, isolate_git_repo_dir, tmp_path, config_path
):
    """Real db_handler + real created_handle (not mocked): a hint set for
    the event's path must reach the git commit's author."""
    import yaml
    from vcs.services.actor_hints import consume_hint

    monkeypatch.setattr(local_consumer, "consume_actor_hint", consume_hint)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({"sources": [{"type": "local", "path": str(tmp_path)}]}))

    watched = tmp_path / "doc.txt"
    watched.write_text("hello")
    set_hint(db_handler, str(watched), "agent:sess-9f3a")

    consumer = LocalConsumer(db_handler=db_handler)
    event = CreatedEvent(src=str(watched))

    consumer.handle(event)

    assert event.actor == "agent:sess-9f3a"
    import vcs.services.mirror_path as mp
    repo_path, relpath = mp.resolve_mirror_location(str(watched), [str(tmp_path)])
    info = git_store.commit_info(repo_path, relpath)
    assert info.author == "agent:sess-9f3a <agent@chrono-ctx.local>"


def test_ec2_handle_leaves_actor_none_when_no_pending_hint(
    monkeypatch, db_handler, isolate_git_repo_dir, tmp_path, config_path
):
    import yaml
    from vcs.services.actor_hints import consume_hint

    monkeypatch.setattr(local_consumer, "consume_actor_hint", consume_hint)
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({"sources": [{"type": "local", "path": str(tmp_path)}]}))

    watched = tmp_path / "doc.txt"
    watched.write_text("hello")

    consumer = LocalConsumer(db_handler=db_handler)
    event = CreatedEvent(src=str(watched))

    consumer.handle(event)

    assert event.actor is None
