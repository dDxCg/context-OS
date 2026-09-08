import shutil
from pathlib import Path

from fastmcp import Context, FastMCP

from app.mcp.guardrail import ensure_scope
from utils.helper import read_text_file, save_to_file
from vcs.services.versioning import current_version

mcp = FastMCP("chrono-ctx")

DENIED = {"status": "denied", "reason": "path out of scope"}

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
# the same change.

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
async def write_file(path: str, content: str, ctx: Context):
    grant = await ensure_scope(ctx, path)
    if not grant:
        return DENIED
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
async def delete_file(path: str, ctx: Context):
    grant = await ensure_scope(ctx, path)
    if not grant:
        return DENIED
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
