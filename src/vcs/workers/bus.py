"""In-process publish/subscribe bus.

Shaped after an AMQP topic exchange so that swapping in RabbitMQ later is a
broker change rather than a rewrite:

    EventBus      -> the exchange
    Subscription  -> a queue plus its binding
    pattern       -> the binding key ("source.#")
    EventBroker   -> the queue itself (LocalQueue), unchanged

Producers publish once and know nothing about how many consumers exist or which
mailbox each one owns. That coupling is what made it possible to publish a
single STOP for two blocked readers (issues.md #1); close() now fans STOP out to
every subscription, so shutdown cannot drift out of sync with the consumer set.
"""

import logging
from abc import ABC, abstractmethod

from vcs.shared.types import topic_for
from vcs.workers.local.local_queue import LocalQueue
from vcs.workers.utils import STOP


def topic_matches(pattern: str, topic: str) -> bool:
    """AMQP topic-exchange matching.

    `*` matches exactly one word, `#` matches zero or more. Words are
    dot-separated.

    The zero-or-more part matters here: "source.#" matches the bare key
    "source" that is published today *and* a finer "source.created" published
    later, so routing can be refined without rebinding anything.
    """
    p = pattern.split(".")
    t = topic.split(".")

    # match[i][j] -> does p[:i] match t[:j]
    match = [[False] * (len(t) + 1) for _ in range(len(p) + 1)]
    match[0][0] = True

    for i in range(1, len(p) + 1):
        if p[i - 1] == "#":
            match[i][0] = match[i - 1][0]

    for i in range(1, len(p) + 1):
        for j in range(1, len(t) + 1):
            if p[i - 1] == "#":
                # consume zero words, or consume one more
                match[i][j] = match[i - 1][j] or match[i][j - 1]
            elif p[i - 1] == "*" or p[i - 1] == t[j - 1]:
                match[i][j] = match[i - 1][j - 1]

    return match[len(p)][len(t)]


class Subscription:
    """A bound mailbox: what RabbitMQ would call a queue plus its binding.

    `where` is an optional predicate the *subscriber* declares to narrow what it
    accepts beyond the routing key. It is evaluated by the bus during fanout,
    on the publishing thread - see LocalEventBus.publish.
    """

    __slots__ = ("pattern", "queue", "where")

    def __init__(self, pattern: str, queue, where=None):
        self.pattern = pattern
        self.queue = queue
        self.where = where

    def accepts(self, topic: str, event) -> bool:
        if not topic_matches(self.pattern, topic):
            return False
        if self.where is None:
            return True
        return bool(self.where(event))

    def consume(self):
        return self.queue.consume()


class EventBus(ABC):
    @abstractmethod
    def publish(self, event):
        pass

    @abstractmethod
    def subscribe(self, pattern, queue=None, where=None) -> Subscription:
        pass

    @abstractmethod
    def close(self):
        pass


class LocalEventBus(EventBus):
    """Single-process fanout onto per-subscriber queues.

    Delivery is deliberately asynchronous - publish() only enqueues. Running
    handlers inline would execute them on watchdog's dispatcher thread while it
    holds BaseObserver._lock, which puts DB writes on the watcher thread and
    deadlocks against ConfigConsumer's watcher.reconcile(), which needs that
    same lock. For the same reason the mailboxes must stay unbounded: a
    blocking put() on that thread would deadlock identically.
    """

    def __init__(self, queue_cls=LocalQueue):
        self.queue_cls = queue_cls
        self.subscriptions: list[Subscription] = []
        self.closed = False

    def subscribe(self, pattern, queue=None, where=None) -> Subscription:
        subscription = Subscription(
            pattern=pattern,
            queue=queue if queue is not None else self.queue_cls(),
            where=where,
        )
        self.subscriptions.append(subscription)
        return subscription

    def publish(self, event):
        """Fan `event` out to every subscription whose binding accepts it.

        A predicate raising must not kill the producer - that producer is the
        watchdog dispatcher thread, and losing it would silently stop all
        watching.
        """
        topic = topic_for(event)
        for subscription in self.subscriptions:
            try:
                accepted = subscription.accepts(topic, event)
            except Exception:
                logging.exception(
                    "[BUS] predicate failed for %s; dropping event", subscription.pattern
                )
                continue
            if accepted:
                subscription.queue.publish(event)

    def close(self):
        """Signal every subscriber to stop, then close their mailboxes.

        Broadcasting here is the point: shutdown no longer depends on the
        runtime remembering how many queues exist, which is the bug class
        behind issues.md #1.
        """
        if self.closed:
            return
        self.closed = True
        for subscription in self.subscriptions:
            subscription.queue.publish(STOP)
