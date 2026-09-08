import pytest

from vcs.shared.types import (
    CONFIG_TOPIC,
    SOURCE_TOPIC,
    ConfigCreatedEvent,
    ConfigDeletedEvent,
    ConfigModifiedEvent,
    ConfigMovedEvent,
    CreatedEvent,
    DeletedEvent,
    ModifiedEvent,
    MovedEvent,
    event_from_dict,
    event_name,
    event_to_dict,
    topic_for,
)

SOURCE_SAMPLES = [
    CreatedEvent(src="/a/x.txt"),
    ModifiedEvent(src="/a/x.txt"),
    DeletedEvent(src="/a/x.txt"),
    MovedEvent(src="/a/x.txt", dst="/a/y.txt"),
]

CONFIG_SAMPLES = [
    ConfigCreatedEvent(src="/cfg/config.yaml"),
    ConfigModifiedEvent(src="/cfg/config.yaml"),
    ConfigDeletedEvent(src="/cfg/config.yaml"),
    ConfigMovedEvent(src="/cfg/config.yaml", dst="/cfg/old.yaml"),
]


@pytest.mark.parametrize("event", SOURCE_SAMPLES, ids=lambda e: type(e).__name__)
def test_source_events_route_to_the_source_topic(event):
    assert topic_for(event) == SOURCE_TOPIC


@pytest.mark.parametrize("event", CONFIG_SAMPLES, ids=lambda e: type(e).__name__)
def test_config_events_route_to_the_config_topic(event):
    """Config events subclass the plain events, so this must not fall through
    to the source topic."""
    assert topic_for(event) == CONFIG_TOPIC


@pytest.mark.parametrize(
    "event", SOURCE_SAMPLES + CONFIG_SAMPLES, ids=lambda e: type(e).__name__
)
def test_round_trip_preserves_class_and_fields(event):
    restored = event_from_dict(event_to_dict(event))

    assert type(restored) is type(event)
    assert restored.src == event.src
    assert restored.provider == event.provider
    assert restored.is_dir == event.is_dir


@pytest.mark.parametrize(
    "event",
    [MovedEvent(src="/a/x.txt", dst="/a/y.txt"),
     ConfigMovedEvent(src="/cfg/config.yaml", dst="/cfg/old.yaml")],
    ids=lambda e: type(e).__name__,
)
def test_round_trip_preserves_dst(event):
    """MovedEvent is kw_only and carries dst; losing it would silently turn a
    rename into a no-op on the far side of a broker."""
    assert event_from_dict(event_to_dict(event)).dst == event.dst


@pytest.mark.parametrize("event", CONFIG_SAMPLES, ids=lambda e: type(e).__name__)
def test_config_round_trip_keeps_the_original_src(event, monkeypatch):
    """Config*Event defaults src from a factory reading the *current* config
    path. from_dict must pass src explicitly, or a round-trip rewrites it to
    whatever CONFIG_PATH happens to be on the consuming side."""
    monkeypatch.setenv("CONFIG_PATH", "/somewhere/else/config.yaml")

    restored = event_from_dict(event_to_dict(event))

    assert restored.src == "/cfg/config.yaml"


def test_event_name_is_finer_than_the_routing_key():
    """Wire names already carry the verb, so refining routing later needs no
    new vocabulary."""
    assert event_name(ModifiedEvent(src="/a")) == "source.modified"
    assert event_name(ConfigMovedEvent(src="/c", dst="/d")) == "config.moved"


def test_unknown_event_name_is_rejected():
    with pytest.raises(ValueError):
        event_from_dict({"event": "source.exploded", "src": "/a"})


def test_unregistered_event_type_is_rejected():
    class Rogue(ModifiedEvent):
        pass

    with pytest.raises(ValueError):
        event_name(Rogue(src="/a"))


def test_ac1_actor_defaults_to_none_and_round_trips_backward_compatibly():
    event = CreatedEvent(src="/a/x.txt")
    assert event.actor is None

    restored = event_from_dict(event_to_dict(event))
    assert restored.actor is None

    # a dict serialized before "actor" existed has no such key at all
    legacy_dict = {"event": "source.created", "src": "/a/x.txt", "provider": "local", "is_dir": False}
    assert event_from_dict(legacy_dict).actor is None


def test_ac2_actor_round_trips_when_set():
    event = CreatedEvent(src="/a/x.txt", actor="agent:sess-9f3a")

    restored = event_from_dict(event_to_dict(event))

    assert restored.actor == "agent:sess-9f3a"
