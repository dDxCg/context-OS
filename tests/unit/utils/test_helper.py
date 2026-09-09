import sys
from pathlib import Path


import utils.helper as helper
from utils.helper import (
    PROJECT_ROOT,
    anchored,
    get_config_path,
    get_db_url,
    get_schema_path,
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


def test_ac1_resolve_project_root_uses_chrono_ctx_home_override(monkeypatch, tmp_path):
    override = tmp_path / "custom-home"
    monkeypatch.setenv("CHRONO_CTX_HOME", str(override))

    result = helper._resolve_project_root(tmp_path / "unrelated-candidate")

    assert result == override.resolve()


def test_ac2_resolve_project_root_uses_candidate_when_it_is_a_source_checkout(monkeypatch, tmp_path):
    monkeypatch.delenv("CHRONO_CTX_HOME", raising=False)
    (tmp_path / "pyproject.toml").write_text("")

    result = helper._resolve_project_root(tmp_path)

    assert result == tmp_path


def test_ac3_resolve_project_root_falls_back_to_user_data_dir_when_no_marker(monkeypatch, tmp_path):
    monkeypatch.delenv("CHRONO_CTX_HOME", raising=False)
    no_marker_candidate = tmp_path / "site-packages" / "utils"
    no_marker_candidate.mkdir(parents=True)

    result = helper._resolve_project_root(no_marker_candidate)

    assert result == helper._default_user_data_dir()


def test_default_user_data_dir_windows_uses_localappdata(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\someone\AppData\Local")

    result = helper._default_user_data_dir()

    assert result == Path(r"C:\Users\someone\AppData\Local") / "chrono-ctx"


def test_default_user_data_dir_macos_uses_application_support(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")

    result = helper._default_user_data_dir()

    assert result == Path.home() / "Library" / "Application Support" / "chrono-ctx"


def test_default_user_data_dir_linux_respects_xdg_data_home(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", "/custom/data")

    result = helper._default_user_data_dir()

    assert result == Path("/custom/data") / "chrono-ctx"


def test_default_user_data_dir_linux_falls_back_to_local_share(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    result = helper._default_user_data_dir()

    assert result == Path.home() / ".local" / "share" / "chrono-ctx"


def test_ac4_get_db_url_defaults_in_dev_mode_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MODE", "dev")

    assert get_db_url() == str(tmp_path / "data" / "db-dev.sqlite")


def test_ac5_get_db_url_defaults_in_prod_mode_when_unconfigured(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "PROJECT_ROOT", tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MODE", "prod")

    assert get_db_url() == str(tmp_path / "data" / "db.sqlite")


def test_ac6_get_db_url_still_honors_explicit_database_url(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("DATABASE_URL", "explicit.sqlite")

    assert get_db_url() == str(tmp_path / "explicit.sqlite")


def test_ac1_get_schema_path_resolves_via_importlib_resources_when_unset(monkeypatch):
    """schema.sql is packaged code (spec 030), not PROJECT_ROOT-anchored
    user data - must resolve correctly regardless of cwd/PROJECT_ROOT."""
    monkeypatch.delenv("SCHEMA_PATH", raising=False)

    result = get_schema_path()

    assert Path(result).is_file()
    assert Path(result).read_text() == Path("src/vcs/db/schema.sql").read_text()


def test_ac2_get_schema_path_still_honors_explicit_schema_path_override(monkeypatch, tmp_path):
    custom = tmp_path / "custom-schema.sql"
    custom.write_text("CREATE TABLE custom (id INTEGER);")
    monkeypatch.setattr(helper, "PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("SCHEMA_PATH", "custom-schema.sql")

    assert get_schema_path() == str(custom)


def test_ac1_default_mode_is_dev_for_a_source_checkout(tmp_path):
    (tmp_path / "pyproject.toml").write_text("")

    assert helper._default_mode(tmp_path) == "dev"


def test_ac2_default_mode_is_prod_for_a_packaged_install(tmp_path):
    no_marker_candidate = tmp_path / "site-packages" / "utils"
    no_marker_candidate.mkdir(parents=True)

    assert helper._default_mode(no_marker_candidate) == "prod"


def test_ac1_get_db_url_defaults_to_dev_db_in_a_source_checkout(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(helper, "_DEFAULT_MODE", "dev")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODE", raising=False)

    assert get_db_url() == str(tmp_path / "data" / "db-dev.sqlite")


def test_ac2_get_db_url_defaults_to_prod_db_in_a_packaged_install(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(helper, "_DEFAULT_MODE", "prod")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("MODE", raising=False)

    assert get_db_url() == str(tmp_path / "data" / "db.sqlite")


def test_ac3_get_db_url_explicit_mode_still_wins_in_a_packaged_install(monkeypatch, tmp_path):
    monkeypatch.setattr(helper, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(helper, "_DEFAULT_MODE", "prod")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MODE", "dev")

    assert get_db_url() == str(tmp_path / "data" / "db-dev.sqlite")
