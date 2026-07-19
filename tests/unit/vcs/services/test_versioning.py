import pytest

import vcs.services.versioning as versioning
from vcs.services.versioning import (
    created_handle,
    deleted_handle,
    modified_handle,
    moved_handle,
    sync_source_status,
)
from vcs.shared.temp_file import TempFile
from vcs.shared.types import CreatedEvent, DeletedEvent, ModifiedEvent, MovedEvent, Query
from utils.helper import gen_hash, get_path_stats


@pytest.fixture(autouse=True)
def isolate_blob_dir(tmp_path, monkeypatch):
    blob_dir = tmp_path / "blobs"
    blob_dir.mkdir()
    monkeypatch.setattr(versioning, "BLOB_DIR", blob_dir)
    return blob_dir


@pytest.fixture(autouse=True)
def isolate_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(TempFile, "TMP_DIR", tmp_path / "tmp")


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


def _insert_version(db_handler, context_id, version_number, content_hash):
    db_handler.execute(Query(
        "INSERT INTO versions (version_number, context_id, content_hash) VALUES (?, ?, ?)",
        (version_number, context_id, content_hash),
    ))


def test_created_handle_inserts_context_location_and_version(db_handler, tmp_path):
    watched_file = tmp_path / "doc.txt"
    watched_file.write_text("hello world")

    created_handle(db_handler, CreatedEvent(src=str(watched_file)))

    contexts = db_handler.execute(Query("SELECT context_id FROM contexts"), commit=False)
    locations = db_handler.execute(Query("SELECT location, status FROM locations"), commit=False)
    versions = db_handler.execute(Query("SELECT version_number, content_hash FROM versions"), commit=False)

    assert len(contexts) == 1
    assert locations == [(str(watched_file), 1)]
    assert versions == [(1, gen_hash(b"hello world"))]


def test_deleted_handle_marks_location_inactive(db_handler, seeder):
    seeder.seed_locations()

    deleted_handle(db_handler, DeletedEvent(src="/root"))

    rows = db_handler.execute(
        Query("SELECT status FROM locations WHERE location = ?", ("/root",)), commit=False
    )
    assert rows == [(0,)]


def test_moved_handle_updates_location_by_matching_inode(db_handler, tmp_path):
    original = tmp_path / "orig.txt"
    original.write_text("data")

    _insert_context(db_handler, "ctx-moved")
    _insert_location(db_handler, original, "ctx-moved")

    renamed = tmp_path / "renamed.txt"
    original.rename(renamed)

    moved_handle(db_handler, MovedEvent(src=str(original), dst=str(renamed)))

    rows = db_handler.execute(
        Query("SELECT location FROM locations WHERE context_id = ?", ("ctx-moved",)), commit=False
    )
    assert rows == [(str(renamed),)]


def test_modified_handle_creates_context_when_none_tracked_yet(db_handler, tmp_path):
    watched = tmp_path / "new.txt"
    watched.write_text("brand new content")

    modified_handle(db_handler, ModifiedEvent(src=str(watched)), tmp_file=None)

    contexts = db_handler.execute(Query("SELECT context_id FROM contexts"), commit=False)
    versions = db_handler.execute(Query("SELECT content_hash FROM versions"), commit=False)
    assert len(contexts) == 1
    assert versions == [(gen_hash(b"brand new content"),)]


def test_modified_handle_appends_version_when_similarity_below_threshold(
    db_handler, tmp_path, isolate_blob_dir
):
    watched = tmp_path / "doc.txt"
    watched.write_text("original content")

    _insert_context(db_handler, "ctx-a")
    _insert_location(db_handler, watched, "ctx-a")
    old_hash = gen_hash(b"original content")
    _insert_version(db_handler, "ctx-a", 1, old_hash)
    (isolate_blob_dir / f"{old_hash}.blob").write_bytes(b"original content")

    new_text = "a totally different payload with enough new words to drop similarity"
    watched.write_text(new_text)
    tmp_file = TempFile.from_path(str(watched))

    modified_handle(db_handler, ModifiedEvent(src=str(watched)), tmp_file)

    versions = db_handler.execute(
        Query("SELECT version_number FROM versions WHERE context_id = ?", ("ctx-a",)), commit=False
    )
    assert len(versions) == 2


def test_modified_handle_stores_new_content_under_its_own_hash(
    db_handler, tmp_path, isolate_blob_dir
):
    watched = tmp_path / "doc.txt"
    watched.write_text("original content")

    _insert_context(db_handler, "ctx-a")
    _insert_location(db_handler, watched, "ctx-a")
    old_hash = gen_hash(b"original content")
    _insert_version(db_handler, "ctx-a", 1, old_hash)
    (isolate_blob_dir / f"{old_hash}.blob").write_bytes(b"original content")

    new_text = "a totally different payload with enough new words to drop similarity"
    watched.write_text(new_text)
    tmp_file = TempFile.from_path(str(watched))

    modified_handle(db_handler, ModifiedEvent(src=str(watched)), tmp_file)

    new_hash = gen_hash(new_text.encode())
    versions = db_handler.execute(
        Query(
            "SELECT content_hash FROM versions WHERE context_id = ? AND version_number = 2",
            ("ctx-a",),
        ),
        commit=False,
    )
    assert versions == [(new_hash,)]
    assert (isolate_blob_dir / f"{new_hash}.blob").exists()
    assert (isolate_blob_dir / f"{new_hash}.blob").read_bytes() == new_text.encode()


def test_modified_handle_skips_version_when_similarity_above_threshold(
    db_handler, tmp_path, isolate_blob_dir
):
    original_text = "original content that is fairly long so a tiny edit keeps it similar"
    watched = tmp_path / "doc.txt"
    watched.write_text(original_text)

    _insert_context(db_handler, "ctx-b")
    _insert_location(db_handler, watched, "ctx-b")
    old_hash = gen_hash(original_text.encode())
    _insert_version(db_handler, "ctx-b", 1, old_hash)
    (isolate_blob_dir / f"{old_hash}.blob").write_bytes(original_text.encode())

    watched.write_text(original_text + "!")
    tmp_file = TempFile.from_path(str(watched))
    tmp_file_path = tmp_file.path

    modified_handle(db_handler, ModifiedEvent(src=str(watched)), tmp_file)

    versions = db_handler.execute(
        Query("SELECT version_number FROM versions WHERE context_id = ?", ("ctx-b",)), commit=False
    )
    assert len(versions) == 1
    assert not tmp_file_path.exists()


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
