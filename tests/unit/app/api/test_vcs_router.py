import sqlite3
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

import app.api.server as server
import vcs.services.mirror_path as mirror_path
from app.api.deps import get_db_handler
from vcs.db.sqlite import DBHandler
from vcs.services import git_store


@pytest.fixture(autouse=True)
def isolate_git_repo_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(mirror_path, "GIT_REPO_DIR", tmp_path / "git-repos")


@pytest.fixture
def db_handler():
    """Overrides the shared fixture: TestClient runs the app's sync
    dependencies/endpoints via run_in_threadpool, free to land on a
    different worker thread than this fixture, so the connection needs
    check_same_thread=False (see app/api/deps.py)."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.executescript(Path("data/schema.sql").read_text())
    yield DBHandler(conn=conn)
    conn.close()


def _write_config(path, sources):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"sources": sources}))


def _source_dir(config_path, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source_dir)}])
    return source_dir


@pytest.fixture
def client(db_handler):
    server.app.dependency_overrides[get_db_handler] = lambda: db_handler
    yield TestClient(server.app)
    server.app.dependency_overrides.clear()


def test_ac1_list_sources_returns_tracked_locations(client, db_handler, config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    from vcs.shared.types import Query
    from utils.helper import get_path_stats
    db_handler.execute(Query("INSERT INTO contexts (context_id) VALUES (?)", ("ctx-1",)))
    stats = get_path_stats(str(tracked))
    db_handler.execute(Query(
        """
        INSERT INTO locations (st_ino, st_dev, location, context_id, provider, status)
        VALUES (?, ?, ?, ?, 'local', 1)
        """,
        (stats["st_ino"], stats["st_dev"], str(tracked), "ctx-1"),
    ))

    response = client.get("/v1/sources")

    assert response.status_code == 200
    assert response.json() == [
        {"location": str(tracked), "provider": "local", "status": 1, "version": None}
    ]


def test_ac2_history_returns_versions_for_in_scope_path(client, config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    rev = git_store.write(repo_path, relpath, b"hello", message="seed", author="t <t@chrono-ctx.local>")

    response = client.get("/v1/history", params={"path": str(tracked)})

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == str(tracked)
    assert [v["rev"] for v in body["versions"]] == [rev]


def test_ac3_diff_returns_unified_diff_for_in_scope_path(client, config_path, tmp_path):
    source_dir = _source_dir(config_path, tmp_path)
    tracked = source_dir / "doc.txt"
    tracked.write_text("hello")
    watch_targets = [str(source_dir)]
    repo_path, relpath = mirror_path.resolve_mirror_location(str(tracked), watch_targets)
    git_store.init_repo(repo_path)
    v1 = git_store.write(repo_path, relpath, b"line one\n", message="add", author="t <t@chrono-ctx.local>")
    v2 = git_store.write(repo_path, relpath, b"line two\n", message="update", author="t <t@chrono-ctx.local>")

    response = client.get("/v1/diff", params={"path": str(tracked), "v1": v1, "v2": v2})

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == str(tracked)
    assert "-line one" in body["diff"]
    assert "+line two" in body["diff"]


def test_ec1_history_out_of_scope_returns_403(client, config_path, tmp_path):
    _source_dir(config_path, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    response = client.get("/v1/history", params={"path": str(outside)})

    assert response.status_code == 403


def test_ec1_diff_out_of_scope_returns_403(client, config_path, tmp_path):
    _source_dir(config_path, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("nope")

    response = client.get("/v1/diff", params={"path": str(outside), "v1": "aaa", "v2": "bbb"})

    assert response.status_code == 403
