import pytest

from vcs.shared.types import ConfigModifiedEvent, ModifiedEvent
from vcs.workers.bus import LocalEventBus, topic_matches
from vcs.workers.utils import STOP


@pytest.mark.parametrize("pattern,topic,expected", [
    # "#" matches zero or more words. The zero case is load-bearing: it is why
    # "source.#" can bind today's bare "source" key and a future
    # "source.created" without rebinding.
    ("source.#", "source", True),
    ("source.#", "source.created", True),
    ("source.#", "source.a.b", True),
    ("source.#", "config", False),
    ("config.#", "config", True),
    ("config.#", "config.modified", True),
    ("config.#", "source", False),
    # exact
    ("source", "source", True),
    ("source", "source.created", False),
    # "*" matches exactly one word
    ("source.*", "source.created", True),
    ("source.*", "source", False),
    ("source.*", "source.a.b", False),
    ("*", "source", True),
    ("#", "anything.at.all", True),
])
def test_topic_matches(pattern, topic, expected):
    assert topic_matches(pattern, topic) is expected


def test_publish_fans_out_to_every_matching_subscription():
    bus = LocalEventBus()
    a = bus.subscribe("source.#")
    b = bus.subscribe("source.#")
    event = ModifiedEvent(src="/a/x.txt")

    bus.publish(event)

    assert a.consume() is event
    assert b.consume() is event


def test_publish_skips_non_matching_subscriptions():
    bus = LocalEventBus()
    source = bus.subscribe("source.#")
    config = bus.subscribe("config.#")

    bus.publish(ModifiedEvent(src="/a/x.txt"))

    assert source.queue.queue.qsize() == 1
    assert config.queue.queue.empty()


def test_config_event_routes_to_the_config_binding():
    bus = LocalEventBus()
    source = bus.subscribe("source.#")
    config = bus.subscribe("config.#")
    event = ConfigModifiedEvent(src="/cfg/config.yaml")

    bus.publish(event)

    assert config.consume() is event
    assert source.queue.queue.empty()


def test_where_predicate_filters_within_a_binding():
    bus = LocalEventBus()
    sub = bus.subscribe("source.#", where=lambda e: e.src.endswith(".md"))

    bus.publish(ModifiedEvent(src="/a/keep.md"))
    bus.publish(ModifiedEvent(src="/a/drop.txt"))

    assert sub.consume().src == "/a/keep.md"
    assert sub.queue.queue.empty()


def test_a_failing_predicate_does_not_kill_the_publisher():
    """publish() runs on watchdog's dispatcher thread; letting an exception
    escape would silently stop all watching."""
    def boom(event):
        raise RuntimeError("predicate exploded")

    bus = LocalEventBus()
    bad = bus.subscribe("source.#", where=boom)
    good = bus.subscribe("source.#")
    event = ModifiedEvent(src="/a/x.txt")

    bus.publish(event)

    assert bad.queue.queue.empty()
    assert good.consume() is event


def test_close_broadcasts_stop_to_every_subscription():
    bus = LocalEventBus()
    subs = [bus.subscribe("source.#"), bus.subscribe("config.#"), bus.subscribe("#")]

    bus.close()

    for sub in subs:
        assert sub.consume() is STOP


def test_close_is_idempotent():
    bus = LocalEventBus()
    sub = bus.subscribe("#")

    bus.close()
    bus.close()

    assert sub.consume() is STOP
    assert sub.queue.queue.empty(), "second close() must not enqueue a second STOP"


def test_subscribe_accepts_an_existing_queue():
    """ConsumerWorker owns its mailbox in some wirings; the bus must be able to
    bind to it rather than always creating one."""
    from vcs.workers.local.local_queue import LocalQueue

    bus = LocalEventBus()
    mailbox = LocalQueue()
    sub = bus.subscribe("source.#", queue=mailbox)

    assert sub.queue is mailbox
    bus.publish(ModifiedEvent(src="/a/x.txt"))
    assert mailbox.queue.qsize() == 1
