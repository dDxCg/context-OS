import yaml
import pytest
from fastmcp import Client
from fastmcp.client.elicitation import ElicitResult

import app.mcp.server as server
from utils.helper import path_normalize


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
