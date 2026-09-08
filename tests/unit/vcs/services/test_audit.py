import pytest
import yaml

import vcs.services.audit as audit
import vcs.services.mirror_path as mirror_path
from vcs.services import git_store
from vcs.shared.types import Query
from utils.helper import get_path_stats


@pytest.fixture(autouse=True)
def isolate_git_repo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror_path, "GIT_REPO_DIR", tmp_path / "git-repos")


def _write_config(path, sources):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"sources": sources}))


def _insert_location(db_handler, path, context_id, status=1):
    db_handler.execute(Query("INSERT INTO contexts (context_id) VALUES (?)", (context_id,)))
    stats = get_path_stats(str(path))
    db_handler.execute(Query(
        """
        INSERT INTO locations (st_ino, st_dev, location, context_id, provider, status)
        VALUES (?, ?, ?, ?, 'local', ?)
        """,
        (stats["st_ino"], stats["st_dev"], str(path), context_id, status),
    ))


def _source_dir(config_path, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source_dir)}])
    return source_dir


def test_ac1_get_sources_lists_locations_with_current_version(db_handler, config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    _insert_location(db_handler, tracked, "ctx-1")

    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    expected_rev = git_store.write(
        repo_path, relpath, b"hello", message="seed", author="t <t@chrono-ctx.local>",
    )

    sources = audit.get_sources(db_handler, watch_targets=watch_targets)

    assert sources == [
        {"location": str(tracked), "provider": "local", "status": 1, "version": expected_rev}
    ]


def test_ac2_get_version_list_returns_history_newest_first(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    first_rev = git_store.write(repo_path, relpath, b"v1", message="add", author="t <t@chrono-ctx.local>")
    second_rev = git_store.write(repo_path, relpath, b"v2", message="update", author="t <t@chrono-ctx.local>")

    history = audit.get_version_list(str(tracked), watch_targets=watch_targets)

    assert [h["rev"] for h in history] == [second_rev, first_rev]
    assert history[0]["message"] == "update"


def test_ac3_check_diff_returns_unified_diff(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    v1 = git_store.write(repo_path, relpath, b"line one\n", message="add", author="t <t@chrono-ctx.local>")
    v2 = git_store.write(repo_path, relpath, b"line two\n", message="update", author="t <t@chrono-ctx.local>")

    text = audit.check_diff(str(tracked), v1, v2, watch_targets=watch_targets)

    assert "-line one" in text
    assert "+line two" in text


def test_ec1_get_version_list_out_of_scope_raises(config_path, tmp_path):
    _source_dir(config_path, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    with pytest.raises(audit.OutOfScopeError):
        audit.get_version_list(str(outside))


def test_ec1_check_diff_out_of_scope_raises(config_path, tmp_path):
    _source_dir(config_path, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    with pytest.raises(audit.OutOfScopeError):
        audit.check_diff(str(outside), "aaa", "bbb")


def test_ec2_get_version_list_no_history_returns_empty_list(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    never_committed = source_dir / "never.txt"
    never_committed.write_text("hi")

    assert audit.get_version_list(str(never_committed)) == []


def test_ec2_check_diff_no_history_returns_empty_string(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    never_committed = source_dir / "never.txt"
    never_committed.write_text("hi")

    assert audit.check_diff(str(never_committed), "aaa", "bbb") == ""
