import sqlite3
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import app.cli.app as cli_app
import vcs.services.mirror_path as mirror_path
from vcs.services import git_store
from utils.helper import get_path_stats

runner = CliRunner()


@pytest.fixture(autouse=True)
def isolate_git_repo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror_path, "GIT_REPO_DIR", tmp_path / "git-repos")


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = tmp_path / "test.sqlite"
    conn = sqlite3.connect(str(path))
    conn.executescript(Path("data/schema.sql").read_text())
    conn.close()
    monkeypatch.setattr(cli_app, "get_db_url", lambda: str(path))
    return path


def _write_config(path, sources):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"sources": sources}))


def _source_dir(config_path, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source_dir)}])
    return source_dir


def _seed_location(db_path, location, context_id="ctx-1", status=1):
    stats = get_path_stats(location)
    conn = sqlite3.connect(str(db_path))
    conn.execute("INSERT INTO contexts (context_id) VALUES (?)", (context_id,))
    conn.execute(
        """
        INSERT INTO locations (st_ino, st_dev, location, context_id, provider, status)
        VALUES (?, ?, ?, ?, 'local', ?)
        """,
        (stats["st_ino"], stats["st_dev"], location, context_id, status),
    )
    conn.commit()
    conn.close()


def test_ac1_source_list_prints_tracked_locations(db_path, config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    _seed_location(db_path, str(tracked))

    result = runner.invoke(cli_app.cli, ["source", "list"])

    assert result.exit_code == 0
    assert str(tracked) in result.stdout
    assert "status=1" in result.stdout
    assert "version=-" in result.stdout


def test_ac2_source_add_and_remove_print_confirmation(config_path, tmp_path):
    _write_config(config_path, [])
    target = tmp_path / "docs"
    target.mkdir()

    add_result = runner.invoke(cli_app.cli, ["source", "add", str(target)])
    assert add_result.exit_code == 0
    assert "added" in add_result.stdout
    assert str(target) in add_result.stdout

    remove_result = runner.invoke(cli_app.cli, ["source", "remove", str(target)])
    assert remove_result.exit_code == 0
    assert "removed" in remove_result.stdout
    assert str(target) in remove_result.stdout


def test_ac3_history_prints_versions_newest_first(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    first_rev = git_store.write(repo_path, relpath, b"v1", message="add", author="t <t@chrono-ctx.local>")
    second_rev = git_store.write(repo_path, relpath, b"v2", message="update", author="t <t@chrono-ctx.local>")

    result = runner.invoke(cli_app.cli, ["history", str(tracked)])

    assert result.exit_code == 0
    lines = [line for line in result.stdout.strip().splitlines() if line]
    assert lines[0].startswith(second_rev)
    assert lines[1].startswith(first_rev)


def test_ec1_history_out_of_scope_exits_nonzero(config_path, tmp_path):
    _source_dir(config_path, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    result = runner.invoke(cli_app.cli, ["history", str(outside)])

    assert result.exit_code != 0


def test_ac4_diff_without_revs_uses_two_most_recent_versions(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(repo_path, relpath, b"line one\n", message="add", author="t <t@chrono-ctx.local>")
    git_store.write(repo_path, relpath, b"line two\n", message="update", author="t <t@chrono-ctx.local>")

    result = runner.invoke(cli_app.cli, ["diff", str(tracked)])

    assert result.exit_code == 0
    assert "-line one" in result.stdout
    assert "+line two" in result.stdout


def test_ac5_diff_with_explicit_revs(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    v1 = git_store.write(repo_path, relpath, b"line one\n", message="add", author="t <t@chrono-ctx.local>")
    v2 = git_store.write(repo_path, relpath, b"line two\n", message="update", author="t <t@chrono-ctx.local>")

    result = runner.invoke(cli_app.cli, ["diff", str(tracked), "--from", v1, "--to", v2])

    assert result.exit_code == 0
    assert "-line one" in result.stdout
    assert "+line two" in result.stdout


def test_ec2_diff_with_only_one_rev_exits_nonzero(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")

    result = runner.invoke(cli_app.cli, ["diff", str(tracked), "--from", "abc123"])

    assert result.exit_code != 0


def test_ec3_diff_with_insufficient_history_exits_nonzero(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(repo_path, relpath, b"only version", message="add", author="t <t@chrono-ctx.local>")

    result = runner.invoke(cli_app.cli, ["diff", str(tracked)])

    assert result.exit_code != 0


def test_ac6_rollback_restores_old_content_and_prints_new_rev(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("v1")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    v1_rev = git_store.write(repo_path, relpath, b"v1", message="add", author="t <t@chrono-ctx.local>")
    git_store.write(repo_path, relpath, b"v2", message="update", author="t <t@chrono-ctx.local>")
    tracked.write_text("v2")

    result = runner.invoke(cli_app.cli, ["rollback", str(tracked), "--version", v1_rev])

    assert result.exit_code == 0
    assert tracked.read_bytes() == b"v1"
    assert v1_rev in result.stdout


def test_ec4_rollback_missing_version_exits_nonzero(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hi")

    result = runner.invoke(cli_app.cli, ["rollback", str(tracked)])

    assert result.exit_code != 0


def test_ec5_rollback_out_of_scope_exits_nonzero(config_path, tmp_path):
    _source_dir(config_path, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    result = runner.invoke(cli_app.cli, ["rollback", str(outside), "--version", "aaa"])

    assert result.exit_code != 0


def test_ac7_rollback_session_restores_and_reports_each_path(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(repo_path, relpath, b"v0", message="add", author="human:alice <human@chrono-ctx.local>")
    git_store.write(repo_path, relpath, b"v1", message="session", author="agent:s1 <agent@chrono-ctx.local>")
    tracked.write_bytes(b"v1")

    result = runner.invoke(cli_app.cli, ["rollback-session", "agent:s1"])

    assert result.exit_code == 0
    assert relpath in result.stdout
    assert tracked.read_bytes() == b"v0"


def test_ec6_rollback_session_refuses_unknown_filesystem():
    result = runner.invoke(cli_app.cli, ["rollback-session", "unknown:filesystem"])

    assert result.exit_code != 0
