import time

from vcs.services import actor_hints


def test_ac1_set_then_consume_returns_the_actor(db_handler):
    actor_hints.set_hint(db_handler, "/watched/doc.txt", "agent:sess-1")

    assert actor_hints.consume_hint(db_handler, "/watched/doc.txt") == "agent:sess-1"


def test_ac2_consume_is_destructive(db_handler):
    actor_hints.set_hint(db_handler, "/watched/doc.txt", "agent:sess-1")
    actor_hints.consume_hint(db_handler, "/watched/doc.txt")

    assert actor_hints.consume_hint(db_handler, "/watched/doc.txt") is None


def test_ac3_consume_with_no_hint_returns_none(db_handler):
    assert actor_hints.consume_hint(db_handler, "/never/hinted.txt") is None


def test_ac4_consume_past_ttl_returns_none_and_deletes_row(db_handler):
    actor_hints.set_hint(db_handler, "/watched/doc.txt", "agent:sess-1", ttl_seconds=0.01)
    time.sleep(0.02)

    assert actor_hints.consume_hint(db_handler, "/watched/doc.txt") is None
    # deleted, not just expired-and-left-behind - a fresh hint afterwards
    # must not collide with a stale row
    actor_hints.set_hint(db_handler, "/watched/doc.txt", "agent:sess-2")
    assert actor_hints.consume_hint(db_handler, "/watched/doc.txt") == "agent:sess-2"
