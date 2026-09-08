import signal

import pytest

import vcs.initialize as initialize_module
import vcs.services.db as db_module
import vcs.services.mirror_path as mirror_path
import vcs.shared.config as shared_config
from vcs.runtime import VCSRuntime


@pytest.fixture
def isolated_runtime(tmp_path, config_path, config_snapshot_file, monkeypatch):
    """Redirects every real-filesystem side effect VCSRuntime.__init__/run()
    touches (db, git mirror dir, config snapshot dir) into tmp_path, matching
    tests/unit/vcs/test_initialize.py's isolated_bootstrap fixture.

    local_runtime.run()/.stop() are stubbed out - they would otherwise start
    real watcher/consumer threads (LocalRuntime's own behavior, already
    covered by tests/integration/vcs/workers/local/test_local_runtime_shutdown.py).
    These tests are about VCSRuntime's own signal-handling orchestration."""
    db_path = str(tmp_path / "db.sqlite")
    monkeypatch.setattr(initialize_module, "get_db_url", lambda: db_path)
    monkeypatch.setattr(db_module, "get_db_url", lambda: db_path)
    monkeypatch.setattr(shared_config, "CONFIG_SNAPSHOT_DIR", config_snapshot_file.parent)
    monkeypatch.setattr(mirror_path, "GIT_REPO_DIR", tmp_path / "git-repos")

    runtime = VCSRuntime()
    monkeypatch.setattr(runtime.local_runtime, "run", lambda: None)
    monkeypatch.setattr(runtime.local_runtime, "stop", _CallCounter())

    yield runtime

    # Registering a real SIGTERM handler in one test must not leak into
    # others - restore the interpreter's default before the next test runs.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)


class _CallCounter:
    def __init__(self):
        self.calls = 0

    def __call__(self, *args, **kwargs):
        self.calls += 1


def test_ac1_run_registers_sigterm_handler_before_blocking(isolated_runtime):
    isolated_runtime.run()

    assert signal.getsignal(signal.SIGTERM) == isolated_runtime._handle_signal


def test_ac2_handle_signal_sets_stop_event(isolated_runtime):
    assert not isolated_runtime.stop_event.is_set()

    isolated_runtime._handle_signal(signal.SIGTERM, None)

    assert isolated_runtime.stop_event.is_set()


def test_ac3_stop_called_twice_does_not_raise(isolated_runtime):
    isolated_runtime.stop()

    isolated_runtime.stop()  # must not raise on the second call

    assert isolated_runtime.local_runtime.stop.calls == 1
