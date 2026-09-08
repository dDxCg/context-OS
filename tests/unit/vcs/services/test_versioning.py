import pytest

import subprocess

import vcs.services.mirror_path as mirror_path
from vcs.services.git_store import head_rev
from vcs.services.versioning import (
    _resolve_actor,
    created_handle,
    deleted_handle,
    modified_handle,
    moved_handle,
    sync_source_status,
)
from vcs.shared.temp_file import TempFile
from vcs.shared.types import CreatedEvent, DeletedEvent, ModifiedEvent, MovedEvent, Query
from utils.helper import get_path_stats, path_normalize


@pytest.fixture(autouse=True)
def isolate_git_repo_dir(tmp_path, monkeypatch):
    repo_dir = tmp_path / "git-repos"
    monkeypatch.setattr(mirror_path, "GIT_REPO_DIR", repo_dir)
    return repo_dir


@pytest.fixture(autouse=True)
def isolate_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(TempFile, "TMP_DIR", tmp_path / "tmp")


def _log_count(repo_path):
    import subprocess
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--format=%H"],
        capture_output=True, text=True, check=True,
    )
    lines = result.stdout.strip().splitlines()
    return len(lines)


def _insert_context(db_handler, context_id):
    db_handler.execute(Query("INSERT INTO contexts (context_id) VALUES (?)", (context_id,)))


def _insert_location(db_handler, path, context_id):
    stats = get_path_stats(str(path))
    db_handler.execute(Query(
        """
        INSERT INTO locations (st_ino, st_dev, location, context_id, status)
        VALUES (?, ?, ?, ?, 1)
        """,
        (stats["st_ino"], stats["st_dev"], str(path), context_id),
    ))
    return stats
    return stats


def test_ac1_created_handle_commits_to_git_not_versions_table(db_handler, tmp_path):
    watched_file = tmp_path / "doc.txt"
    watched_file.write_text("hello world")

    created_handle(
        db_handler, CreatedEvent(src=str(watched_file)), watch_targets=[str(tmp_path)]
    )

    contexts = db_handler.execute(Query("SELECT context_id FROM contexts"), commit=False)
    locations = db_handler.execute(Query("SELECT location, status FROM locations"), commit=False)
    versions = db_handler.execute(Query("SELECT version_number, content_hash FROM versions"), commit=False)

    assert len(contexts) == 1
    assert locations == [(str(watched_file), 1)]
    assert versions == []

    repo_path, relpath = mirror_path.resolve_mirror_location(str(watched_file), [str(tmp_path)])
    assert head_rev(repo_path, relpath) is not None
    assert (repo_path / relpath).read_bytes() == b"hello world"


def test_ac2_created_handle_recreate_same_content_no_duplicate_commit(db_handler, tmp_path):
    watched_file = tmp_path / "doc.txt"
    watched_file.write_text("hello world")

    created_handle(
        db_handler, CreatedEvent(src=str(watched_file)), watch_targets=[str(tmp_path)]
    )
    repo_path, relpath = mirror_path.resolve_mirror_location(str(watched_file), [str(tmp_path)])
    first_rev = head_rev(repo_path, relpath)

    # delete-then-recreate cycle: same content, called again
    created_handle(
        db_handler, CreatedEvent(src=str(watched_file)), watch_targets=[str(tmp_path)]
    )

    assert head_rev(repo_path, relpath) == first_rev
    locations = db_handler.execute(Query("SELECT status FROM locations"), commit=False)
    assert locations == [(1,)]


def test_ac2_deleted_handle_marks_location_inactive_and_commits_removal(db_handler, tmp_path):
    watched = tmp_path / "doc.txt"
    watched.write_text("hello world")
    created_handle(db_handler, CreatedEvent(src=str(watched)), watch_targets=[str(tmp_path)])
    repo_path, relpath = mirror_path.resolve_mirror_location(str(watched), [str(tmp_path)])
    rev_before = head_rev(repo_path, relpath)

    deleted_handle(db_handler, DeletedEvent(src=str(watched)), watch_targets=[str(tmp_path)])

    rows = db_handler.execute(
        Query("SELECT status FROM locations WHERE location = ?", (str(watched),)), commit=False
    )
    assert rows == [(0,)]
    assert not (repo_path / relpath).exists()
    rev_after = head_rev(repo_path, relpath)
    assert rev_after is not None
    assert rev_after != rev_before


def test_ac4_deleted_handle_on_untracked_path_is_noop(db_handler, tmp_path):
    other = tmp_path / "never-tracked.txt"

    deleted_handle(db_handler, DeletedEvent(src=str(other)), watch_targets=[str(tmp_path)])

    repo_path, relpath = mirror_path.resolve_mirror_location(str(other), [str(tmp_path)])
    assert head_rev(repo_path, relpath) is None


def test_ac1_moved_handle_renames_within_same_watch_target(db_handler, tmp_path):
    original = tmp_path / "orig.txt"
    original.write_text("data")
    created_handle(db_handler, CreatedEvent(src=str(original)), watch_targets=[str(tmp_path)])

    renamed = tmp_path / "renamed.txt"
    original.rename(renamed)

    moved_handle(
        db_handler, MovedEvent(src=str(original), dst=str(renamed)), watch_targets=[str(tmp_path)]
    )

    repo_path, dst_relpath = mirror_path.resolve_mirror_location(str(renamed), [str(tmp_path)])
    _, src_relpath = mirror_path.resolve_mirror_location(str(original), [str(tmp_path)])
    assert (repo_path / dst_relpath).read_bytes() == b"data"
    assert not (repo_path / src_relpath).exists()

    import subprocess
    log = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--follow", "--format=%H", "--", dst_relpath],
        capture_output=True, text=True, check=True,
    )
    assert len(log.stdout.strip().splitlines()) == 2


def test_ac3_moved_handle_across_watch_targets_writes_dst_and_removes_src(db_handler, tmp_path):
    source_a = tmp_path / "a"
    source_a.mkdir()
    source_b = tmp_path / "b"
    source_b.mkdir()
    watch_targets = [str(source_a), str(source_b)]

    original = source_a / "doc.txt"
    original.write_text("cross-repo data")
    created_handle(db_handler, CreatedEvent(src=str(original)), watch_targets=watch_targets)

    moved = source_b / "doc.txt"
    original.rename(moved)

    moved_handle(
        db_handler, MovedEvent(src=str(original), dst=str(moved)), watch_targets=watch_targets
    )

    src_repo, src_relpath = mirror_path.resolve_mirror_location(str(original), watch_targets)
    dst_repo, dst_relpath = mirror_path.resolve_mirror_location(str(moved), watch_targets)
    assert src_repo != dst_repo
    assert not (src_repo / src_relpath).exists()
    assert (dst_repo / dst_relpath).read_bytes() == b"cross-repo data"


def test_ec1_deleted_handle_outside_watch_targets_raises_path_not_watched(db_handler, tmp_path):
    with pytest.raises(mirror_path.PathNotWatchedError):
        deleted_handle(
            db_handler, DeletedEvent(src=str(tmp_path / "elsewhere.txt")),
            watch_targets=[str(tmp_path / "watched")],
        )


def test_ac1_deleted_handle_deactivates_directory_subtree(db_handler, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").mkdir()
    (root / "b").mkdir()
    other = tmp_path / "other"
    other.mkdir()
    rootx = tmp_path / "rootx"
    rootx.mkdir()

    _insert_context(db_handler, "ctx-root")
    _insert_location(db_handler, path_normalize(root), "ctx-root")
    _insert_context(db_handler, "ctx-a")
    _insert_location(db_handler, path_normalize(root / "a"), "ctx-a")
    _insert_context(db_handler, "ctx-b")
    _insert_location(db_handler, path_normalize(root / "b"), "ctx-b")
    _insert_context(db_handler, "ctx-other")
    _insert_location(db_handler, path_normalize(other), "ctx-other")
    _insert_context(db_handler, "ctx-rootx")
    _insert_location(db_handler, path_normalize(rootx), "ctx-rootx")

    deleted_handle(
        db_handler, DeletedEvent(src=path_normalize(root)), watch_targets=[str(tmp_path)]
    )

    rows = dict(db_handler.execute(
        Query("SELECT context_id, status FROM locations"), commit=False
    ))
    assert rows["ctx-root"] == 0
    assert rows["ctx-a"] == 0
    assert rows["ctx-b"] == 0
    assert rows["ctx-other"] == 1
    assert rows["ctx-rootx"] == 1


def test_ac3_moved_handle_rewrites_directory_subtree_in_scope(db_handler, tmp_path, config_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "one.txt").write_text("one")
    (root / "a" / "two.txt").write_text("two")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"sources:\n  - type: local\n    path: {path_normalize(root)}\n")

    _insert_context(db_handler, "ctx-one")
    one_stats = _insert_location(db_handler, path_normalize(root / "a" / "one.txt"), "ctx-one")
    _insert_context(db_handler, "ctx-two")
    _insert_location(db_handler, path_normalize(root / "a" / "two.txt"), "ctx-two")

    new_dir = root / "a2"
    (root / "a").rename(new_dir)

    moved_handle(
        db_handler,
        MovedEvent(src=path_normalize(root / "a"), dst=path_normalize(new_dir)),
        watch_targets=[str(root)],
    )

    raw_rows = db_handler.execute(
        Query("SELECT context_id, location, status FROM locations"), commit=False
    )
    rows = {context_id: (location, status) for context_id, location, status in raw_rows}
    assert rows["ctx-one"] == (path_normalize(new_dir / "one.txt"), 1)
    assert rows["ctx-two"] == (path_normalize(new_dir / "two.txt"), 1)

    ino_row = db_handler.execute(
        Query("SELECT st_ino, st_dev FROM locations WHERE context_id = ?", ("ctx-one",)),
        commit=False,
    )
    assert ino_row == [(one_stats["st_ino"], one_stats["st_dev"])]


def test_ac4_moved_handle_rewrites_directory_subtree_out_of_scope(db_handler, tmp_path, config_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "a").mkdir()
    (root / "a" / "one.txt").write_text("one")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"sources:\n  - type: local\n    path: {path_normalize(root)}\n")

    _insert_context(db_handler, "ctx-one")
    _insert_location(db_handler, path_normalize(root / "a" / "one.txt"), "ctx-one")

    outside = tmp_path / "outside"
    outside.mkdir()
    new_dir = outside / "a"
    (root / "a").rename(new_dir)

    moved_handle(
        db_handler,
        MovedEvent(src=path_normalize(root / "a"), dst=path_normalize(new_dir)),
        watch_targets=[str(root)],
    )

    rows = dict(
        (context_id, (location, status))
        for context_id, location, status in db_handler.execute(
            Query("SELECT context_id, location, status FROM locations"), commit=False
        )
    )
    assert rows["ctx-one"] == (path_normalize(new_dir / "one.txt"), 0)


def test_ac5_moved_handle_file_rename_sets_status_from_scope(db_handler, tmp_path, config_path):
    root = tmp_path / "root"
    root.mkdir()
    original = root / "orig.txt"
    original.write_text("data")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"sources:\n  - type: local\n    path: {path_normalize(root)}\n")

    _insert_context(db_handler, "ctx-moved")
    stats_before = _insert_location(db_handler, path_normalize(original), "ctx-moved")

    renamed = root / "renamed.txt"
    original.rename(renamed)

    moved_handle(
        db_handler,
        MovedEvent(src=path_normalize(original), dst=path_normalize(renamed)),
        watch_targets=[str(root)],
    )

    rows = db_handler.execute(
        Query(
            "SELECT location, st_ino, st_dev, status FROM locations WHERE context_id = ?",
            ("ctx-moved",),
        ),
        commit=False,
    )
    assert rows == [(path_normalize(renamed), stats_before["st_ino"], stats_before["st_dev"], 1)]


def test_ec1_moved_handle_dst_missing_on_disk_does_not_raise(db_handler, tmp_path, config_path):
    root = tmp_path / "root"
    root.mkdir()
    original = root / "orig.txt"
    original.write_text("data")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(f"sources:\n  - type: local\n    path: {path_normalize(root)}\n")

    _insert_context(db_handler, "ctx-raced")
    _insert_location(db_handler, path_normalize(original), "ctx-raced")

    # dst named in the event no longer exists - raced by a second
    # move/delete before this handler ran.
    already_gone = path_normalize(root / "already-gone.txt")

    moved_handle(
        db_handler,
        MovedEvent(src=path_normalize(original), dst=already_gone),
        watch_targets=[str(root)],
    )  # must not raise


def test_ac5_modified_handle_creates_context_when_none_tracked_yet(db_handler, tmp_path):
    watched = tmp_path / "new.txt"
    watched.write_text("brand new content")

    modified_handle(
        db_handler, ModifiedEvent(src=str(watched)), tmp_file=None, watch_targets=[str(tmp_path)]
    )

    contexts = db_handler.execute(Query("SELECT context_id FROM contexts"), commit=False)
    versions = db_handler.execute(Query("SELECT content_hash FROM versions"), commit=False)
    assert len(contexts) == 1
    assert versions == []
    repo_path, relpath = mirror_path.resolve_mirror_location(str(watched), [str(tmp_path)])
    assert (repo_path / relpath).read_bytes() == b"brand new content"


def test_ac3_modified_handle_commits_when_similarity_below_threshold(
    db_handler, tmp_path
):
    watched = tmp_path / "doc.txt"
    watched.write_text("original content")
    created_handle(
        db_handler, CreatedEvent(src=str(watched)), watch_targets=[str(tmp_path)]
    )
    repo_path, relpath = mirror_path.resolve_mirror_location(str(watched), [str(tmp_path)])
    first_rev = head_rev(repo_path, relpath)

    new_text = "a totally different payload with enough new words to drop similarity"
    watched.write_text(new_text)
    tmp_file = TempFile.from_path(str(watched))

    modified_handle(
        db_handler, ModifiedEvent(src=str(watched)), tmp_file, watch_targets=[str(tmp_path)]
    )

    versions = db_handler.execute(Query("SELECT * FROM versions"), commit=False)
    assert versions == []
    assert _log_count(repo_path) == 2
    assert head_rev(repo_path, relpath) != first_rev
    assert (repo_path / relpath).read_bytes() == new_text.encode()


def test_ac4_modified_handle_skips_commit_when_similarity_above_threshold(
    db_handler, tmp_path
):
    original_text = "original content that is fairly long so a tiny edit keeps it similar"
    watched = tmp_path / "doc.txt"
    watched.write_text(original_text)
    created_handle(
        db_handler, CreatedEvent(src=str(watched)), watch_targets=[str(tmp_path)]
    )
    repo_path, relpath = mirror_path.resolve_mirror_location(str(watched), [str(tmp_path)])

    watched.write_text(original_text + "!")
    tmp_file = TempFile.from_path(str(watched))
    tmp_file_path = tmp_file.path

    modified_handle(
        db_handler, ModifiedEvent(src=str(watched)), tmp_file, watch_targets=[str(tmp_path)]
    )

    assert _log_count(repo_path) == 1
    assert not tmp_file_path.exists()


def test_ec1_created_handle_outside_watch_targets_raises_path_not_watched(db_handler, tmp_path):
    watched_file = tmp_path / "elsewhere" / "doc.txt"
    watched_file.parent.mkdir()
    watched_file.write_text("hello")

    other_dir = tmp_path / "watched"
    other_dir.mkdir()

    with pytest.raises(mirror_path.PathNotWatchedError):
        created_handle(
            db_handler, CreatedEvent(src=str(watched_file)), watch_targets=[str(other_dir)]
        )


def test_ac3_resolve_actor_formats_git_author_from_actor_string():
    event = CreatedEvent(src="/a/x.txt", actor="agent:sess-9f3a")

    label, author = _resolve_actor(event)

    assert label == "agent:sess-9f3a"
    assert author == "agent:sess-9f3a <agent@chrono-ctx.local>"


def test_ac4_resolve_actor_defaults_when_actor_is_none():
    event = CreatedEvent(src="/a/x.txt")

    label, author = _resolve_actor(event)

    assert label == "unknown:filesystem"
    assert author == "unknown:filesystem <unknown@chrono-ctx.local>"


def test_ac5_created_handle_uses_event_actor_in_author_and_message(db_handler, tmp_path):
    watched_file = tmp_path / "doc.txt"
    watched_file.write_text("hello")

    created_handle(
        db_handler, CreatedEvent(src=str(watched_file), actor="cli:jane"),
        watch_targets=[str(tmp_path)],
    )

    repo_path, relpath = mirror_path.resolve_mirror_location(str(watched_file), [str(tmp_path)])
    log = subprocess.run(
        ["git", "-C", str(repo_path), "log", "-1", "--format=%an <%ae>|%s"],
        capture_output=True, text=True, check=True,
    )
    author, message = log.stdout.strip().split("|", 1)
    assert author == "cli:jane <cli@chrono-ctx.local>"
    assert message.endswith("via cli:jane")


def test_ac6_modified_handle_delegation_preserves_actor(db_handler, tmp_path):
    watched_file = tmp_path / "new.txt"
    watched_file.write_text("brand new")

    modified_handle(
        db_handler, ModifiedEvent(src=str(watched_file), actor="cli:jane"), tmp_file=None,
        watch_targets=[str(tmp_path)],
    )

    repo_path, relpath = mirror_path.resolve_mirror_location(str(watched_file), [str(tmp_path)])
    log = subprocess.run(
        ["git", "-C", str(repo_path), "log", "-1", "--format=%an <%ae>"],
        capture_output=True, text=True, check=True,
    )
    assert log.stdout.strip() == "cli:jane <cli@chrono-ctx.local>"


def test_sync_source_status_deactivates_missing_and_reactivates_present(db_handler, tmp_path):
    kept = tmp_path / "kept.txt"
    kept.write_text("keep me")

    _insert_context(db_handler, "ctx-kept")
    _insert_location(db_handler, kept, "ctx-kept")

    _insert_context(db_handler, "ctx-gone")
    db_handler.execute(Query(
        """
        INSERT INTO locations (st_ino, st_dev, location, context_id, status)
        VALUES ('999', '999', ?, ?, 1)
        """,
        ("gone/path", "ctx-gone"),
    ))

    sync_source_status(db_handler, sources=[{"type": "local", "path": str(tmp_path)}])

    rows = dict(db_handler.execute(Query("SELECT context_id, status FROM locations"), commit=False))
    assert rows["ctx-kept"] == 1
    assert rows["ctx-gone"] == 0
