from pathlib import Path

import pytest

from utils.helper import (
    PROJECT_ROOT,
    anchored,
    get_config_path,
    read_file,
    read_text_file,
    save_to_file,
)

NON_ASCII = "# Café — naïve 日本語 🎉\n"


def _write_utf8(path: Path, text: str) -> None:
    """Write exact bytes. Path.write_text() goes through text mode, which
    translates \\n to \\r\\n on Windows - these tests need byte precision."""
    path.write_bytes(text.encode("utf-8"))


def test_read_file_decodes_utf8_written_by_another_writer(tmp_path):
    """The reported corruption: a UTF-8 file read back as the platform
    codepage returned mojibake with no error at all."""
    target = tmp_path / "doc.md"
    _write_utf8(target, NON_ASCII)

    assert read_file(str(target), mode="r") == NON_ASCII


def test_save_to_file_writes_utf8_bytes(tmp_path):
    target = tmp_path / "out.md"

    save_to_file(NON_ASCII, str(target), mode="w")

    assert target.read_bytes().decode("utf-8") == NON_ASCII


def test_text_round_trip_preserves_non_ascii(tmp_path):
    target = tmp_path / "round.md"

    save_to_file(NON_ASCII, str(target), mode="w")

    assert read_file(str(target), mode="r") == NON_ASCII


def test_text_write_does_not_translate_newlines(tmp_path):
    """Text mode would rewrite \\n as \\r\\n on Windows, mutating content and
    producing spurious versions on a read/write round-trip."""
    target = tmp_path / "lf.txt"

    save_to_file("a\nb\nc", str(target), mode="w")

    assert target.read_bytes() == b"a\nb\nc"


def test_text_read_does_not_translate_newlines(tmp_path):
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"a\r\nb")

    assert read_file(str(target), mode="r") == "a\r\nb"


def test_binary_mode_returns_bytes_unchanged(tmp_path):
    target = tmp_path / "blob.bin"
    payload = b"\x89PNG\r\n\x1a\n\x00\x8d\xff\xfe"
    target.write_bytes(payload)

    assert read_file(str(target), mode="rb") == payload


def test_binary_round_trip_ignores_encoding(tmp_path):
    """The versioning path is bytes end-to-end and must be unaffected."""
    target = tmp_path / "blob.bin"
    payload = b"\x00\x01\x02\xff\xfe"

    save_to_file(payload, str(target), mode="wb")

    assert read_file(str(target), mode="rb") == payload


def test_read_text_file_reports_exact_decode(tmp_path):
    target = tmp_path / "doc.md"
    _write_utf8(target, NON_ASCII)

    content, lossy = read_text_file(str(target))

    assert content == NON_ASCII
    assert lossy is False


def test_read_text_file_flags_undecodable_bytes_instead_of_raising(tmp_path):
    target = tmp_path / "image.png"
    target.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x8d\xff\xfe")

    content, lossy = read_text_file(str(target))

    assert lossy is True
    assert "�" in content


def test_read_text_file_does_not_flag_a_literal_replacement_char(tmp_path):
    """No false positives: U+FFFD stored as valid UTF-8 decodes strictly."""
    target = tmp_path / "doc.md"
    _write_utf8(target, "legitimately � here")

    content, lossy = read_text_file(str(target))

    assert content == "legitimately � here"
    assert lossy is False


def test_anchored_leaves_absolute_paths_alone(tmp_path):
    assert anchored(str(tmp_path)) == str(tmp_path)


def test_anchored_resolves_relative_paths_against_project_root(monkeypatch, tmp_path):
    """The MCP server runs over stdio and its cwd is chosen by the client, so
    resolving against cwd lets the two processes use different config files."""
    monkeypatch.chdir(tmp_path)

    assert anchored("config.yaml") == str(PROJECT_ROOT / "config.yaml")


def test_get_config_path_is_stable_across_working_directories(monkeypatch, tmp_path):
    monkeypatch.delenv("CONFIG_PATH", raising=False)
    monkeypatch.chdir(PROJECT_ROOT)
    from_root = get_config_path()
    monkeypatch.chdir(tmp_path)
    from_elsewhere = get_config_path()

    assert from_root == from_elsewhere


def test_get_config_path_defaults_when_unset(monkeypatch):
    """Returning None here detonates later inside path_normalize when the
    Config*Event dataclasses build their default src."""
    monkeypatch.delenv("CONFIG_PATH", raising=False)

    assert get_config_path() is not None
