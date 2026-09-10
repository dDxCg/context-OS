import logging
import shutil
import sqlite3
import subprocess
import time
from functools import wraps
from pathlib import Path

from fastmcp import Context, FastMCP

from app.mcp.guardrail import ensure_scope
from utils.helper import anchored, get_db_url, read_text_file, save_to_file
from vcs.db.sqlite import DBHandler
from vcs.services.actor_hints import set_hint
from vcs.services.versioning import current_version

mcp = FastMCP("chrono-ctx")

DENIED = {"status": "denied", "reason": "path out of scope"}

LOG_PATH = Path(anchored("data/mcp.log"))

# Bookkeeping this module is explicitly willing to skip must not inherit
# DBHandler.from_url()'s 30s busy timeout (spec 038) - waiting half a
# minute for something optional is the wrong trade, and it silently added
# that much to a call that then succeeded anyway.
HINT_DB_TIMEOUT: float = 2.0


def _setup_file_logging() -> None:
    """A stdio MCP server has nowhere durable to log: stdout is the
    JSON-RPC protocol (a handler there corrupts the stream) and stderr
    goes wherever the client puts it, which is nowhere. So: an explicit
    file handler, alongside the daemon's own data/ctx.log.

    Never fatal - a server that refuses to start because it can't log is
    strictly worse than one that runs unlogged.
    """
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(LOG_PATH, encoding="utf-8")
        handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root = logging.getLogger()
        root.addHandler(handler)
        root.setLevel(logging.INFO)
    except OSError:
        pass


def _logged_tool(func):
    """Entry/exit with elapsed time, so a future hang is diagnosable from
    the log alone - which one wasn't, before spec 038.

    Paths only, never content: these are the user's context sources, and
    logging arguments would turn this file into a plaintext copy of every
    watched document.
    """
    @wraps(func)
    async def wrapper(*args, **kwargs):
        target = kwargs.get("path") or kwargs.get("src") or ""
        logging.info("[MCP] %s start path=%s", func.__name__, target)
        start = time.monotonic()
        try:
            result = await func(*args, **kwargs)
        except Exception:
            logging.exception(
                "[MCP] %s raised after %.3fs path=%s",
                func.__name__, time.monotonic() - start, target,
            )
            raise
        logging.info(
            "[MCP] %s done status=%s in %.3fs path=%s",
            func.__name__, result.get("status"), time.monotonic() - start, target,
        )
        return result
    return wrapper


def _commit_grants(*grants):
    """grant.commit() rewrites config.yaml *after* the operation already
    landed on disk. A failure here must not report the operation as
    failed - an agent told its write failed will retry a write that
    already happened (spec 038). Reported as a distinct field instead, so
    the next call re-prompting for approval is explainable."""
    try:
        for grant in grants:
            grant.commit()
    except Exception as e:
        logging.warning("[MCP] scope persistence failed: %s", e)
        return {"scope_persisted": False, "scope_error": str(e)}
    return {}


# Every failure path returns a status dict rather than raising, so the caller
# always gets a structured result. shutil.Error and UnicodeError are listed
# explicitly because neither is an OSError subclass - UnicodeDecodeError and
# UnicodeEncodeError derive from ValueError - and both escaped as transport
# level errors before. Deliberately not `except Exception`, so genuine
# programming errors still surface loudly. filelock.Timeout (spec 036/issue
# #29 - the cross-process mirror-repo lock is now timeout-bounded instead
# of blocking forever) needs no special-casing here: it's already an
# OSError subclass. subprocess.TimeoutExpired (spec 037's per-git-call
# bound) is not - it derives from SubprocessError - so it is listed
# explicitly, or a timed-out git escapes as a transport-level error.
IO_ERRORS = (OSError, UnicodeError, shutil.Error, subprocess.TimeoutExpired)


def _check_expected_version(path: str, expected_version: str | None):
    """Optimistic-concurrency pre-check (spec 021). Only compares against
    current_version() at call time - the watcher commits asynchronously
    after this returns, so this narrows the lost-update window rather than
    eliminating it (see 021-mcp-optimistic-concurrency.md's Context)."""
    if expected_version is None:
        return None
    try:
        actual = current_version(path)
    except IO_ERRORS as e:
        return {"status": "error", "reason": str(e)}
    if actual != expected_version:
        return {
            "status": "conflict",
            "reason": f"{path}: expected version {expected_version!r}, current is {actual!r}",
            "current_version": actual,
        }
    return None

# Plain filesystem I/O only - no direct calls into vcs.services.versioning.
# The watcher (vcs/workers/local/local_watcher.py) already tracks every
# filesystem change independently of who made it (this tool, a human, any
# other process), so duplicating that tracking here would double-process
# the same change. A pending actor hint (docs/specs/013-actor-hints.md) is
# left for the watcher to pick up, so the resulting commit isn't attributed
# to the generic "unknown:filesystem" fallback.


def _set_actor_hint(path: str, ctx: Context) -> None:
    """Best-effort: an MCP call must never fail because hint bookkeeping
    couldn't complete. If the DB/table isn't there yet (MCP started before
    the daemon ever applied schema.sql), this just no-ops - same as no
    hint having been set at all. TypeError is caught alongside sqlite3.Error
    because an unconfigured DATABASE_URL makes get_db_url() return None,
    and sqlite3.connect(None, ...) raises TypeError, not a sqlite3.Error -
    still bookkeeping failing to complete, not a reason to fail the write."""
    try:
        db_handler = DBHandler.from_url(get_db_url(), timeout=HINT_DB_TIMEOUT)
        try:
            set_hint(db_handler, path, f"agent:{ctx.session_id}")
        finally:
            db_handler.close()
    except (sqlite3.Error, TypeError) as e:
        # Still never fails the call - but no longer silently. A
        # permanently broken DB means every write loses actor attribution,
        # which used to be invisible forever (spec 038).
        logging.warning("[MCP] actor hint not recorded for %s: %s", path, e)

@mcp.tool()
@_logged_tool
async def read_file(path: str, ctx: Context):
    """Read file contents"""
    grant = await ensure_scope(ctx, path)
    if not grant:
        return DENIED
    try:
        content, lossy = read_text_file(path)
        version = current_version(path)
    except IO_ERRORS as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok",
        **_commit_grants(grant),
        "content": content,
        "version": version,
        # True when bytes could not be decoded as UTF-8 and were replaced -
        # the file is not text (typically binary). Content is degraded; do
        # not write it back.
        "lossy": lossy
    }

@mcp.tool()
@_logged_tool
async def write_file(path: str, content: str, ctx: Context, expected_version: str | None = None):
    grant = await ensure_scope(ctx, path)
    if not grant:
        return DENIED
    conflict = _check_expected_version(path, expected_version)
    if conflict:
        return conflict
    _set_actor_hint(path, ctx)
    try:
        save_to_file(content, path, mode="w")
    except IO_ERRORS as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok",
        **_commit_grants(grant),
    }

@mcp.tool()
@_logged_tool
async def create_file(path: str, content: str, ctx: Context):
    grant = await ensure_scope(ctx, path)
    if not grant:
        return DENIED
    _set_actor_hint(path, ctx)
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        save_to_file(content, path, mode="w")
    except IO_ERRORS as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok",
        **_commit_grants(grant),
    }

@mcp.tool()
@_logged_tool
async def delete_file(path: str, ctx: Context, expected_version: str | None = None):
    grant = await ensure_scope(ctx, path)
    if not grant:
        return DENIED
    conflict = _check_expected_version(path, expected_version)
    if conflict:
        return conflict
    _set_actor_hint(path, ctx)
    try:
        Path(path).unlink()
    except IO_ERRORS as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok",
        **_commit_grants(grant),
    }

@mcp.tool()
@_logged_tool
async def move_file(src: str, dst: str, ctx: Context):
    src_grant = await ensure_scope(ctx, src)
    if not src_grant:
        return {"status": "denied", "reason": "src out of scope"}
    dst_grant = await ensure_scope(ctx, dst)
    if not dst_grant:
        return {"status": "denied", "reason": "dst out of scope"}
    _set_actor_hint(src, ctx)
    _set_actor_hint(dst, ctx)
    try:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dst)
    except IO_ERRORS as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok",
        **_commit_grants(src_grant, dst_grant),
    }


def main():
    _setup_file_logging()
    mcp.run(transport='stdio')

if __name__ == "__main__":
    main()
