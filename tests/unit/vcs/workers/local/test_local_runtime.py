import threading

import pytest

from vcs.workers.local.local_runtime import LocalRuntime


@pytest.mark.xfail(strict=True, reason=(
    "docs/issues.md#1: ConsumerWorker and ConfigConsumerWorker are wired to "
    "the same LocalQueue instance in LocalRuntime.__init__, so they race for "
    "a single STOP sentinel on shutdown. See "
    "tests/integration/vcs/workers/local/test_local_runtime_shutdown.py for "
    "the end-to-end deadlock this causes."
))
def test_consumer_and_config_consumer_workers_use_independent_queues():
    runtime = LocalRuntime(sources=[], stop_event=threading.Event())

    assert runtime.consumer_worker.queue is not runtime.config_consumer_worker.queue
