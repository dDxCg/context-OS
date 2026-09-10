import logging
import sqlite3
import subprocess
from pathlib import Path

import anyio
import filelock
import yaml
import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

import app.mcp.server as server
import vcs.services.mirror_path as mirror_path
from vcs.services import git_store
from vcs.services.actor_hints import consume_hint
from vcs.services.configure import derive_watch_targets
from utils.helper import path_normalize


@pytest.fixture(autouse=True)
def isolate_git_repo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror_path, "GIT_REPO_DIR", tmp_path / "git-repos")


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(Path("src/vcs/db/schema.sql").read_text())
    conn.close()
    monkeypatch.setattr(server, "get_db_url", lambda: str(path))
    return path


def _write_config(path, sources):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"sources": sources}))


async def _approve(message, response_type, params, ctx):
    return ElicitResult(action="accept", content=True)


async def _decline(message, response_type, params, ctx):
    return ElicitResult(action="decline")


async def _fail_if_called(message, response_type, params, ctx):
    raise AssertionError("elicitation should not be triggered for an in-scope path")


@pytest.fixture
def source_dir(config_path, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source)}])
    return source


@pytest.mark.anyio
async def test_read_file_returns_content_for_in_scope_path(source_dir):
    target = source_dir / "doc.txt"
    target.write_text("hello world")

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("read_file", {"path": str(target)})

    assert result.data == {
        "status": "ok", "content": "hello world", "version": None, "lossy": False
    }


@pytest.mark.anyio
async def test_ac1_read_file_returns_real_version_when_mirror_has_history(source_dir):
    """Spec 008 AC-1: a path already committed into its mirror (e.g. by the
    watcher) reports that commit's rev instead of the hardcoded None."""
    target = source_dir / "doc.txt"
    target.write_text("hello world")
    watch_targets = derive_watch_targets()
    repo_path, relpath = mirror_path.resolve_mirror_location(str(target), watch_targets)
    git_store.init_repo(repo_path)
    expected_rev = git_store.write(
        repo_path, relpath, b"hello world",
        message="seed", author="test <test@chrono-ctx.local>",
    )

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("read_file", {"path": str(target)})

    assert result.data["version"] == expected_rev


@pytest.mark.anyio
async def test_read_file_missing_returns_error(source_dir):
    missing = source_dir / "missing.txt"

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("read_file", {"path": str(missing)})

    assert result.data["status"] == "error"


@pytest.mark.anyio
async def test_write_file_overwrites_existing_content_on_disk(source_dir):
    target = source_dir / "doc.txt"
    target.write_text("old content")

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "write_file", {"path": str(target), "content": "new content"}
        )

    assert result.data == {"status": "ok"}
    assert target.read_text() == "new content"


@pytest.mark.anyio
async def test_ac6_write_file_sets_a_pending_actor_hint(source_dir, db_path):
    """Spec 013: the watcher can't attribute an MCP-triggered edit without
    this - it has no other way to know who made the change."""
    target = source_dir / "doc.txt"
    target.write_text("old content")

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        await client.call_tool("write_file", {"path": str(target), "content": "new content"})

    conn = sqlite3.connect(str(db_path))
    db_handler = server.DBHandler(conn)
    actor = consume_hint(db_handler, str(target))
    conn.close()

    assert actor is not None
    assert actor.startswith("agent:")


@pytest.mark.anyio
async def test_write_file_succeeds_when_database_url_is_unconfigured(source_dir, monkeypatch):
    """Regression: _set_actor_hint's best-effort contract ("an MCP call must
    never fail because hint bookkeeping couldn't complete") only caught
    sqlite3.Error. get_db_url() returning None (DATABASE_URL unset - the
    real state of a CI runner with no .env.dev) makes DBHandler.from_url()
    raise TypeError from sqlite3.connect(None, ...), which escaped uncaught
    and failed the whole write. CI caught this; local runs never did,
    because a real .env.dev always supplied DATABASE_URL."""
    monkeypatch.setattr(server, "get_db_url", lambda: None)
    target = source_dir / "doc.txt"
    target.write_text("old content")

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "write_file", {"path": str(target), "content": "new content"}
        )

    assert result.data == {"status": "ok"}
    assert target.read_text() == "new content"


@pytest.mark.anyio
async def test_ac2_write_file_with_matching_expected_version_succeeds(source_dir):
    target = source_dir / "doc.txt"
    target.write_text("old content")
    watch_targets = derive_watch_targets()
    repo_path, relpath = mirror_path.resolve_mirror_location(str(target), watch_targets)
    git_store.init_repo(repo_path)
    current_rev = git_store.write(
        repo_path, relpath, b"old content",
        message="seed", author="test <test@chrono-ctx.local>",
    )

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "write_file",
            {"path": str(target), "content": "new content", "expected_version": current_rev},
        )

    assert result.data == {"status": "ok"}
    assert target.read_text() == "new content"


@pytest.mark.anyio
async def test_ac3_write_file_with_stale_expected_version_returns_conflict(source_dir, db_path):
    target = source_dir / "doc.txt"
    target.write_text("old content")
    watch_targets = derive_watch_targets()
    repo_path, relpath = mirror_path.resolve_mirror_location(str(target), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(
        repo_path, relpath, b"old content",
        message="seed", author="test <test@chrono-ctx.local>",
    )
    current_rev = git_store.write(
        repo_path, relpath, b"someone else's edit",
        message="someone else", author="other <other@chrono-ctx.local>",
    )

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "write_file",
            {"path": str(target), "content": "my stale edit", "expected_version": "stale-rev"},
        )

    assert result.data["status"] == "conflict"
    assert result.data["current_version"] == current_rev
    assert target.read_text() == "old content"

    conn = sqlite3.connect(str(db_path))
    db_handler = server.DBHandler(conn)
    actor = consume_hint(db_handler, str(target))
    conn.close()
    assert actor is None


@pytest.mark.anyio
async def test_ac4_delete_file_with_matching_expected_version_succeeds(source_dir):
    target = source_dir / "doc.txt"
    target.write_text("content")
    watch_targets = derive_watch_targets()
    repo_path, relpath = mirror_path.resolve_mirror_location(str(target), watch_targets)
    git_store.init_repo(repo_path)
    current_rev = git_store.write(
        repo_path, relpath, b"content",
        message="seed", author="test <test@chrono-ctx.local>",
    )

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "delete_file", {"path": str(target), "expected_version": current_rev}
        )

    assert result.data == {"status": "ok"}
    assert not target.exists()


@pytest.mark.anyio
async def test_ac4_delete_file_with_stale_expected_version_returns_conflict(source_dir):
    target = source_dir / "doc.txt"
    target.write_text("content")
    watch_targets = derive_watch_targets()
    repo_path, relpath = mirror_path.resolve_mirror_location(str(target), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(
        repo_path, relpath, b"content",
        message="seed", author="test <test@chrono-ctx.local>",
    )

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "delete_file", {"path": str(target), "expected_version": "stale-rev"}
        )

    assert result.data["status"] == "conflict"
    assert target.exists()


@pytest.mark.anyio
async def test_ac5_conflict_does_not_persist_the_scope_grant(config_path, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source)}])
    outside = tmp_path / "outside" / "doc.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("hi")
    before = config_path.read_bytes()

    async with Client(server.mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool(
            "write_file",
            {"path": str(outside), "content": "x", "expected_version": "stale-rev"},
        )

    assert result.data["status"] == "conflict"
    assert config_path.read_bytes() == before


@pytest.mark.anyio
async def test_create_file_writes_content_and_makes_parent_dirs(source_dir):
    target = source_dir / "nested" / "doc.txt"

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "create_file", {"path": str(target), "content": "brand new"}
        )

    assert result.data == {"status": "ok"}
    assert target.read_text() == "brand new"


@pytest.mark.anyio
async def test_delete_file_removes_existing_file(source_dir):
    target = source_dir / "doc.txt"
    target.write_text("bye")

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("delete_file", {"path": str(target)})

    assert result.data == {"status": "ok"}
    assert not target.exists()


@pytest.mark.anyio
async def test_delete_file_missing_returns_error(source_dir):
    missing = source_dir / "missing.txt"

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("delete_file", {"path": str(missing)})

    assert result.data["status"] == "error"


@pytest.mark.anyio
async def test_move_file_succeeds_when_both_src_and_dst_in_scope(source_dir):
    src = source_dir / "a.txt"
    dst = source_dir / "b.txt"
    src.write_text("hi")

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("move_file", {"src": str(src), "dst": str(dst)})

    assert result.data == {"status": "ok"}
    assert not src.exists()
    assert dst.read_text() == "hi"


@pytest.mark.anyio
async def test_out_of_scope_approval_persists_to_config_and_succeeds(config_path, source_dir, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("hi")

    async with Client(server.mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool("read_file", {"path": str(outside)})

    assert result.data["status"] == "ok"
    saved = yaml.safe_load(config_path.read_text())
    assert path_normalize(str(outside)) in [s["path"] for s in saved["sources"]]


@pytest.mark.anyio
async def test_out_of_scope_decline_blocks_and_leaves_config_untouched(config_path, source_dir, tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("hi")
    before = config_path.read_text()

    async with Client(server.mcp, elicitation_handler=_decline) as client:
        result = await client.call_tool(
            "write_file", {"path": str(outside), "content": "x"}
        )

    assert result.data == {"status": "denied", "reason": "path out of scope"}
    assert config_path.read_text() == before
    assert not outside.exists() or outside.read_text() == "hi"


@pytest.mark.anyio
async def test_move_file_checks_both_src_and_dst(source_dir, tmp_path):
    in_scope = source_dir / "doc.txt"
    in_scope.write_text("hi")
    outside = tmp_path / "outside.txt"

    async with Client(server.mcp, elicitation_handler=_decline) as client:
        result = await client.call_tool(
            "move_file", {"src": str(in_scope), "dst": str(outside)}
        )

    assert result.data == {"status": "denied", "reason": "dst out of scope"}
    assert in_scope.exists()


@pytest.mark.anyio
async def test_read_file_on_binary_returns_status_dict_not_an_exception(source_dir):
    """UnicodeDecodeError subclasses ValueError, not OSError, so it used to
    escape the tool as a transport-level error."""
    target = source_dir / "image.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x8d\xff\xfe binary")

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("read_file", {"path": str(target)})

    assert result.data["status"] == "ok"
    assert result.data["lossy"] is True


@pytest.mark.anyio
async def test_read_file_round_trips_non_ascii_content(source_dir):
    target = source_dir / "doc.md"
    target.write_bytes("# Café 日本語 🎉".encode("utf-8"))

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("read_file", {"path": str(target)})

    assert result.data["content"] == "# Café 日本語 🎉"
    assert result.data["lossy"] is False


@pytest.mark.anyio
async def test_write_file_persists_non_ascii_as_utf8(source_dir):
    target = source_dir / "out.md"

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "write_file", {"path": str(target), "content": "Café 日本語 🎉"}
        )

    assert result.data["status"] == "ok"
    assert target.read_bytes().decode("utf-8") == "Café 日本語 🎉"


@pytest.mark.anyio
async def test_failed_operation_does_not_persist_the_scope_grant(config_path, tmp_path):
    """The check must precede the operation, but the config write must not:
    a failed read used to permanently widen scope and add a watch target."""
    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source)}])
    outside_missing = tmp_path / "outside" / "missing.txt"
    before = config_path.read_bytes()

    async with Client(server.mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool("read_file", {"path": str(outside_missing)})

    assert result.data["status"] == "error"
    assert config_path.read_bytes() == before


@pytest.mark.anyio
async def test_ac3_read_file_returns_error_instead_of_hanging_when_lock_contended(source_dir, monkeypatch):
    """Spec 036 / issue #29: a contended mirror-repo lock now raises
    filelock.Timeout (an OSError subclass) instead of blocking forever -
    read_file's version lookup must surface that as a clean error, not an
    unhandled exception through the MCP transport."""
    target = source_dir / "doc.txt"
    target.write_text("hello")
    monkeypatch.setattr(
        "app.mcp.server.current_version",
        lambda path, watch_targets=None: (_ for _ in ()).throw(filelock.Timeout("repo lock")),
    )

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("read_file", {"path": str(target)})

    assert result.data["status"] == "error"


@pytest.mark.anyio
async def test_ac4_write_file_returns_error_instead_of_hanging_when_lock_contended(source_dir, monkeypatch):
    """Same as above, via _check_expected_version()'s current_version() call
    - only exercised when expected_version is passed."""
    target = source_dir / "doc.txt"
    target.write_text("hello")
    monkeypatch.setattr(
        "app.mcp.server.current_version",
        lambda path, watch_targets=None: (_ for _ in ()).throw(filelock.Timeout("repo lock")),
    )

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "write_file",
            {"path": str(target), "content": "new", "expected_version": "some-rev"},
        )

    assert result.data["status"] == "error"
    assert target.read_text() == "hello"


def test_ac1_logging_setup_writes_to_a_file_and_never_to_stdout(tmp_path, monkeypatch):
    """stdout carries the JSON-RPC protocol - a handler on it corrupts the
    stream. And stderr from a stdio server goes nowhere durable, which is
    why 'check the log' found nothing after the hang that prompted 038."""
    import sys

    log_path = tmp_path / "mcp.log"
    monkeypatch.setattr(server, "LOG_PATH", log_path)
    root = logging.getLogger()
    before = list(root.handlers)
    try:
        server._setup_file_logging()
        added = [h for h in root.handlers if h not in before]

        assert any(isinstance(h, logging.FileHandler) for h in added)
        for handler in added:
            assert getattr(handler, "stream", None) is not sys.stdout
        logging.getLogger(__name__).warning("probe line")
        for handler in added:
            handler.flush()
        assert "probe line" in log_path.read_text(encoding="utf-8")
    finally:
        for handler in [h for h in root.handlers if h not in before]:
            root.removeHandler(handler)
            handler.close()


def test_ec1_logging_setup_does_not_raise_when_the_path_is_unwritable(tmp_path, monkeypatch):
    """A server that refuses to start because it can't log is strictly
    worse than one that runs unlogged."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setattr(server, "LOG_PATH", blocker / "nested" / "mcp.log")

    server._setup_file_logging()


@pytest.mark.anyio
async def test_ac2_tool_call_logs_entry_and_exit_with_the_path_but_not_content(source_dir, caplog):
    """Paths only - these are the user's context sources, and this file
    would otherwise become a plaintext copy of watched documents."""
    target = source_dir / "doc.txt"

    with caplog.at_level(logging.INFO):
        async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
            await client.call_tool(
                "create_file", {"path": str(target), "content": "SECRET-CONTENT-MARKER"}
            )

    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "create_file" in text
    assert "doc.txt" in text
    assert "SECRET-CONTENT-MARKER" not in text


@pytest.mark.anyio
async def test_ac3_set_actor_hint_uses_a_short_timeout_not_the_30s_default(source_dir, monkeypatch):
    """Spec 038 AC-3: waiting 30s for bookkeeping the code is explicitly
    willing to skip is the wrong trade - it adds up to 30s to a call that
    then succeeds anyway."""
    target = source_dir / "doc.txt"
    target.write_text("old")
    captured = {}

    def fake_from_url(db_url, timeout=30.0):
        captured["timeout"] = timeout
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server.DBHandler, "from_url", staticmethod(fake_from_url))

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool(
            "write_file", {"path": str(target), "content": "new"}
        )

    assert captured["timeout"] == server.HINT_DB_TIMEOUT == 2.0
    # AC-4: best-effort contract unchanged - the write still succeeds.
    assert result.data == {"status": "ok"}


@pytest.mark.anyio
async def test_ac4_set_actor_hint_logs_a_warning_when_it_gives_up(source_dir, monkeypatch, caplog):
    """A permanently broken DB - every write silently losing actor
    attribution - must not stay invisible forever."""
    target = source_dir / "doc.txt"
    target.write_text("old")

    def fake_from_url(db_url, timeout=30.0):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(server.DBHandler, "from_url", staticmethod(fake_from_url))

    with caplog.at_level(logging.WARNING):
        async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
            await client.call_tool("write_file", {"path": str(target), "content": "new"})

    assert any(r.levelno == logging.WARNING for r in caplog.records)


@pytest.mark.anyio
async def test_ac5_ensure_scope_logs_the_exception_when_its_catch_all_fires(config_path, tmp_path, caplog):
    """A bug in the elicitation path and a user clicking 'no' are
    indistinguishable to the caller by design (fail closed) - they must at
    least be distinguishable in the log."""
    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source)}])
    outside = tmp_path / "outside.txt"
    outside.write_text("hi")

    async def _boom(message, response_type, params, ctx):
        raise RuntimeError("elicitation machinery is broken")

    with caplog.at_level(logging.ERROR):
        async with Client(server.mcp, elicitation_handler=_boom) as client:
            result = await client.call_tool("read_file", {"path": str(outside)})

    assert result.data["status"] == "denied"
    assert any("elicitation machinery is broken" in r.getMessage() or r.exc_info
               for r in caplog.records)


@pytest.mark.anyio
async def test_ac6_elicit_that_never_resolves_is_abandoned_and_treated_as_a_decline(
    config_path, tmp_path, monkeypatch
):
    """A client that never renders the prompt must not hold the call open
    forever."""
    import app.mcp.guardrail as guardrail

    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source)}])
    outside = tmp_path / "outside.txt"
    outside.write_text("hi")
    monkeypatch.setattr(guardrail, "ELICIT_TIMEOUT", 0.2)

    async def _never_answers(message, response_type, params, ctx):
        # Only needs to outlast the patched 0.2s timeout. A long sleep here
        # would still be awaited to completion during client teardown even
        # though wait_for already abandoned it - 30s of dead test time.
        await anyio.sleep(1)

    async with Client(server.mcp, elicitation_handler=_never_answers) as client:
        result = await client.call_tool("read_file", {"path": str(outside)})

    assert result.data["status"] == "denied"
    assert config_path.read_text() == yaml.safe_dump(
        {"sources": [{"type": "local", "path": str(source)}]}
    )


@pytest.mark.anyio
async def test_ac7_git_timeout_returns_structured_error_not_a_transport_error(source_dir, monkeypatch):
    """spec 037 bounds git with subprocess.TimeoutExpired, which is NOT an
    OSError subclass (unlike filelock.Timeout) - so it isn't covered by
    IO_ERRORS unless listed explicitly."""
    target = source_dir / "doc.txt"
    target.write_text("hello")
    monkeypatch.setattr(
        "app.mcp.server.current_version",
        lambda path, watch_targets=None: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(["git", "log"], 4.0)
        ),
    )

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        result = await client.call_tool("read_file", {"path": str(target)})

    assert result.data["status"] == "error"


@pytest.mark.anyio
async def test_ac8_scope_persistence_failure_does_not_report_a_successful_write_as_failed(
    config_path, tmp_path, monkeypatch
):
    """grant.commit() rewrites config.yaml *after* the write already
    landed. If it raises, the caller must not be told the write failed -
    an agent acting on that retries a write that already happened."""
    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source)}])
    outside = tmp_path / "outside" / "doc.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)

    import app.mcp.guardrail as guardrail

    def _boom(self):
        raise OSError("config.yaml is read-only")

    monkeypatch.setattr(guardrail.ScopeGrant, "commit", _boom)

    async with Client(server.mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool(
            "create_file", {"path": str(outside), "content": "brand new"}
        )

    assert result.data["status"] == "ok"
    assert outside.read_text() == "brand new"
    assert "scope_persisted" in result.data
    assert result.data["scope_persisted"] is False


@pytest.mark.anyio
async def test_successful_operation_does_persist_the_scope_grant(config_path, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source)}])
    outside = tmp_path / "outside" / "doc.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_bytes(b"hi")

    async with Client(server.mcp, elicitation_handler=_approve) as client:
        result = await client.call_tool("read_file", {"path": str(outside)})

    assert result.data["status"] == "ok"
    saved = yaml.safe_load(config_path.read_text())
    assert path_normalize(str(outside)) in [s["path"] for s in saved["sources"]]
