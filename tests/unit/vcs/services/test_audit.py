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


def test_ac1_rollback_source_restores_old_content_as_new_commit(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("v1")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    v1_rev = git_store.write(repo_path, relpath, b"v1", message="add", author="t <t@chrono-ctx.local>")
    v2_rev = git_store.write(repo_path, relpath, b"v2", message="update", author="t <t@chrono-ctx.local>")

    new_rev = audit.rollback_source(str(tracked), v1_rev, watch_targets=watch_targets)

    assert new_rev != v1_rev
    assert new_rev != v2_rev
    history = git_store.log_history(repo_path, relpath)
    assert [h["rev"] for h in history] == [new_rev, v2_rev, v1_rev]


def test_ac2_rollback_source_writes_content_back_to_real_file(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("v1")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    v1_rev = git_store.write(repo_path, relpath, b"v1", message="add", author="t <t@chrono-ctx.local>")
    git_store.write(repo_path, relpath, b"v2", message="update", author="t <t@chrono-ctx.local>")
    tracked.write_text("v2")

    audit.rollback_source(str(tracked), v1_rev, watch_targets=watch_targets)

    assert tracked.read_bytes() == b"v1"


def test_ec1_rollback_source_out_of_scope_raises(config_path, tmp_path):
    _source_dir(config_path, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    with pytest.raises(audit.OutOfScopeError):
        audit.rollback_source(str(outside), "aaa")


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


def test_ac3_rollback_session_restores_path_actor_modified(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(repo_path, relpath, b"v0", message="add", author="human:alice <human@chrono-ctx.local>")
    git_store.write(repo_path, relpath, b"v1", message="session edit 1", author="agent:s1 <agent@chrono-ctx.local>")
    git_store.write(repo_path, relpath, b"v2", message="session edit 2", author="agent:s1 <agent@chrono-ctx.local>")
    tracked.write_bytes(b"v2")

    result = audit.rollback_session("agent:s1", watch_targets=watch_targets)

    assert result == {"rolled_back": [relpath], "failed": []}
    assert tracked.read_bytes() == b"v0"
    assert git_store.show(repo_path, relpath, git_store.head_rev(repo_path, relpath)) == b"v0"


def test_ac4_rollback_session_deletes_path_actor_created(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "new.txt"
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(repo_path, relpath, b"created", message="create", author="agent:s1 <agent@chrono-ctx.local>")
    tracked.write_bytes(b"created")

    result = audit.rollback_session("agent:s1", watch_targets=watch_targets)

    assert result == {"rolled_back": [relpath], "failed": []}
    assert not tracked.exists()
    assert not git_store.path_exists_at_rev(repo_path, relpath, "HEAD")


def test_ac1_rollback_session_deletes_path_created_in_a_shared_repo_with_prior_history(config_path, tmp_path):
    """Spec 024 AC-1 / issue #28: the repo already has an unrelated commit
    from a different author before the target actor creates a brand-new
    path in it - earliest["parent"] is not None here, unlike
    test_ac4 above, so the fix must check tree membership, not just
    whether a parent commit exists at all."""
    source_dir = _source_dir(config_path, tmp_path)
    unrelated = source_dir / "baseline.txt"
    tracked = source_dir / "new.txt"
    watch_targets = [str(source_dir)]
    repo_path, unrelated_relpath = mirror_path.resolve_mirror_location(str(unrelated), watch_targets)
    _, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(
        repo_path, unrelated_relpath, b"baseline", message="baseline",
        author="human:alice <human@chrono-ctx.local>",
    )
    git_store.write(repo_path, relpath, b"created", message="create", author="agent:s1 <agent@chrono-ctx.local>")
    tracked.write_bytes(b"created")

    result = audit.rollback_session("agent:s1", watch_targets=watch_targets)

    assert result == {"rolled_back": [relpath], "failed": []}
    assert not tracked.exists()
    assert not git_store.path_exists_at_rev(repo_path, relpath, "HEAD")


def test_ac5_rollback_session_spans_multiple_watch_targets(config_path, tmp_path):
    dir1 = tmp_path / "s1"
    dir1.mkdir()
    dir2 = tmp_path / "s2"
    dir2.mkdir()
    _write_config(config_path, [
        {"type": "local", "path": str(dir1)},
        {"type": "local", "path": str(dir2)},
    ])
    watch_targets = [str(dir1), str(dir2)]
    f1 = dir1 / "a.txt"
    f2 = dir2 / "b.txt"
    repo1, rel1 = mirror_path.resolve_mirror_location(str(f1), watch_targets)
    repo2, rel2 = mirror_path.resolve_mirror_location(str(f2), watch_targets)
    git_store.init_repo(repo1)
    git_store.init_repo(repo2)
    git_store.write(repo1, rel1, b"a0", message="add", author="human:alice <human@chrono-ctx.local>")
    git_store.write(repo1, rel1, b"a1", message="session", author="agent:s1 <agent@chrono-ctx.local>")
    git_store.write(repo2, rel2, b"b1", message="session create", author="agent:s1 <agent@chrono-ctx.local>")
    f1.write_bytes(b"a1")
    f2.write_bytes(b"b1")

    result = audit.rollback_session("agent:s1", watch_targets=watch_targets)

    assert sorted(result["rolled_back"]) == sorted([rel1, rel2])
    assert result["failed"] == []
    assert f1.read_bytes() == b"a0"
    assert not f2.exists()


def test_ac6_and_ec3_rollback_session_reports_scope_revoked_as_failed(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    revoked_dir = tmp_path / "revoked"
    revoked_dir.mkdir()
    watch_targets = [str(source_dir), str(revoked_dir)]

    ok_file = source_dir / "ok.txt"
    repo_ok, ok_rel = mirror_path.resolve_mirror_location(str(ok_file), watch_targets)
    git_store.init_repo(repo_ok)
    git_store.write(repo_ok, ok_rel, b"v0", message="add", author="human:alice <human@chrono-ctx.local>")
    git_store.write(repo_ok, ok_rel, b"v1", message="session", author="agent:s1 <agent@chrono-ctx.local>")
    ok_file.write_bytes(b"v1")

    revoked_file = revoked_dir / "gone.txt"
    repo_revoked, revoked_rel = mirror_path.resolve_mirror_location(str(revoked_file), watch_targets)
    git_store.init_repo(repo_revoked)
    git_store.write(repo_revoked, revoked_rel, b"r1", message="session create", author="agent:s1 <agent@chrono-ctx.local>")

    result = audit.rollback_session("agent:s1", watch_targets=watch_targets)

    assert result["rolled_back"] == [ok_rel]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["path"] == revoked_rel
    assert ok_file.read_bytes() == b"v0"


def test_ec4_rollback_session_reports_concurrent_edit_as_failed(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    watch_targets = [str(source_dir)]
    contested = source_dir / "contested.txt"
    ok_file = source_dir / "ok.txt"
    repo_path, contested_rel = mirror_path.resolve_mirror_location(str(contested), watch_targets)
    _, ok_rel = mirror_path.resolve_mirror_location(str(ok_file), watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(repo_path, contested_rel, b"v0", message="add", author="human:alice <human@chrono-ctx.local>")
    git_store.write(repo_path, contested_rel, b"v1", message="session", author="agent:s1 <agent@chrono-ctx.local>")
    git_store.write(repo_path, contested_rel, b"v_other", message="someone else", author="human:bob <human@chrono-ctx.local>")
    git_store.write(repo_path, ok_rel, b"o0", message="add", author="human:alice <human@chrono-ctx.local>")
    git_store.write(repo_path, ok_rel, b"o1", message="session", author="agent:s1 <agent@chrono-ctx.local>")
    ok_file.write_bytes(b"o1")

    result = audit.rollback_session("agent:s1", watch_targets=watch_targets)

    assert result["rolled_back"] == [ok_rel]
    assert len(result["failed"]) == 1
    assert result["failed"][0]["path"] == contested_rel
    assert git_store.show(repo_path, contested_rel, "HEAD") == b"v_other"


def test_ec1_rollback_session_refuses_unknown_filesystem():
    with pytest.raises(audit.InvalidSessionActorError):
        audit.rollback_session("unknown:filesystem")


def test_ec2_rollback_session_returns_empty_result_for_actor_with_no_commits(config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    watch_targets = [str(source_dir)]

    result = audit.rollback_session("agent:nobody", watch_targets=watch_targets)

    assert result == {"rolled_back": [], "failed": []}
