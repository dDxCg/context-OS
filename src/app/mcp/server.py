import shutil
import sqlite3
from pathlib import Path

from fastmcp import Context, FastMCP

from app.mcp.guardrail import ensure_scope
from utils.helper import get_db_url, read_text_file, save_to_file
from vcs.db.sqlite import DBHandler
from vcs.services.actor_hints import set_hint
from vcs.services.versioning import current_version

mcp = FastMCP("chrono-ctx")

DENIED = {"status": "denied", "reason": "path out of scope"}


def _check_expected_version(path: str, expected_version: str | None):
    """Optimistic-concurrency pre-check (spec 021). Only compares against
    current_version() at call time - the watcher commits asynchronously
    after this returns, so this narrows the lost-update window rather than
    eliminating it (see 021-mcp-optimistic-concurrency.md's Context)."""
    if expected_version is None:
        return None
    actual = current_version(path)
    if actual != expected_version:
        return {
            "status": "conflict",
            "reason": f"{path}: expected version {expected_version!r}, current is {actual!r}",
            "current_version": actual,
        }
    return None

# Every failure path returns a status dict rather than raising, so the caller
# always gets a structured result. shutil.Error and UnicodeError are listed
# explicitly because neither is an OSError subclass - UnicodeDecodeError and
# UnicodeEncodeError derive from ValueError - and both escaped as transport
# level errors before. Deliberately not `except Exception`, so genuine
# programming errors still surface loudly.
IO_ERRORS = (OSError, UnicodeError, shutil.Error)

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
    hint having been set at all."""
    try:
        db_handler = DBHandler.from_url(get_db_url())
        try:
            set_hint(db_handler, path, f"agent:{ctx.session_id}")
        finally:
            db_handler.close()
    except sqlite3.Error:
        pass

@mcp.tool()
async def read_file(path: str, ctx: Context):
    """Read file contents"""
    grant = await ensure_scope(ctx, path)
    if not grant:
        return DENIED
    try:
        content, lossy = read_text_file(path)
    except IO_ERRORS as e:
        return {"status": "error", "reason": str(e)}
    grant.commit()
    return {
        "status": "ok",
        "content": content,
        "version": current_version(path),
        # True when bytes could not be decoded as UTF-8 and were replaced -
        # the file is not text (typically binary). Content is degraded; do
        # not write it back.
        "lossy": lossy
    }

@mcp.tool()
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
    grant.commit()
    return {
        "status": "ok"
    }

@mcp.tool()
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
    grant.commit()
    return {
        "status": "ok"
    }

@mcp.tool()
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
    grant.commit()
    return {
        "status": "ok"
    }

@mcp.tool()
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
    src_grant.commit()
    dst_grant.commit()
    return {
        "status": "ok"
    }


def main():
    mcp.run(transport='stdio')

if __name__ == "__main__":
    main()
