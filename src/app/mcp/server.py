import shutil
from pathlib import Path

from fastmcp import Context, FastMCP

from app.mcp.guardrail import ensure_scope
from utils.helper import read_file as _read_file, save_to_file

mcp = FastMCP("chrono-ctx")

DENIED = {"status": "denied", "reason": "path out of scope"}

# Plain filesystem I/O only - no direct calls into vcs.services.versioning.
# The watcher (vcs/workers/local/local_watcher.py) already tracks every
# filesystem change independently of who made it (this tool, a human, any
# other process), so duplicating that tracking here would double-process
# the same change.

@mcp.tool()
async def read_file(path: str, ctx: Context):
    """Read file contents"""
    if not await ensure_scope(ctx, path):
        return DENIED
    try:
        content = _read_file(path, mode="r")
    except OSError as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok",
        "content": content,
        "version": None
    }

@mcp.tool()
async def write_file(path: str, content: str, ctx: Context):
    if not await ensure_scope(ctx, path):
        return DENIED
    try:
        save_to_file(content, path, mode="w")
    except OSError as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok"
    }

@mcp.tool()
async def create_file(path: str, content: str, ctx: Context):
    if not await ensure_scope(ctx, path):
        return DENIED
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        save_to_file(content, path, mode="w")
    except OSError as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok"
    }

@mcp.tool()
async def delete_file(path: str, ctx: Context):
    if not await ensure_scope(ctx, path):
        return DENIED
    try:
        Path(path).unlink()
    except OSError as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok"
    }

@mcp.tool()
async def move_file(src: str, dst: str, ctx: Context):
    if not await ensure_scope(ctx, src):
        return {"status": "denied", "reason": "src out of scope"}
    if not await ensure_scope(ctx, dst):
        return {"status": "denied", "reason": "dst out of scope"}
    try:
        Path(dst).parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dst)
    except OSError as e:
        return {"status": "error", "reason": str(e)}
    return {
        "status": "ok"
    }


def main():
    mcp.run(transport='stdio')

if __name__ == "__main__":
    main()
