import subprocess

import yaml
import pytest

import vcs.initialize as initialize_module
import vcs.services.db as db_module
import vcs.services.mirror_path as mirror_path
import vcs.shared.config as shared_config
from vcs.initialize import Initializer
from vcs.services import git_store
from vcs.services.git_store import commit_info, path_exists_at_rev, show
from vcs.shared.types import Query
from utils.helper import path_normalize


def _log_count(repo_path):
    result = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--format=%H"],
        capture_output=True, text=True, check=True,
    )
    return len(result.stdout.strip().splitlines())


@pytest.fixture
def isolated_bootstrap(tmp_path, config_snapshot_file, monkeypatch):
    """Redirects every real-filesystem side effect Initializer.init() touches
    (db file, git mirror dir, config snapshot dir) into tmp_path."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setattr(initialize_module, "get_db_url", lambda: str(db_path))
    monkeypatch.setattr(db_module, "get_db_url", lambda: str(db_path))

    git_repo_dir = tmp_path / "git-repos"
    monkeypatch.setattr(shared_config, "CONFIG_SNAPSHOT_DIR", config_snapshot_file.parent)
    monkeypatch.setattr(mirror_path, "GIT_REPO_DIR", git_repo_dir)

    return {"db_path": db_path, "git_repo_dir": git_repo_dir}


def test_init_bootstraps_schema_config_snapshot_and_local_sources(
    config_path, config_snapshot_file, isolated_bootstrap, tmp_path
):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "doc.txt").write_text("hello world")

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({
        "sources": [{"type": "local", "path": str(source_dir)}]
    }))

    initializer = Initializer()
    try:
        initializer.init()

        contexts = initializer.db_handler.execute(Query("SELECT context_id FROM contexts"), commit=False)
        versions = initializer.db_handler.execute(Query("SELECT content_hash FROM versions"), commit=False)
        assert len(contexts) == 1
        assert versions == []

        repo_path, relpath = mirror_path.resolve_mirror_location(
            str(source_dir / "doc.txt"), [str(source_dir)]
        )
        assert show(repo_path, relpath, "HEAD") == b"hello world"

        assert config_snapshot_file.exists()
        snapshot = yaml.safe_load(config_snapshot_file.read_text())
        assert len(snapshot["sources"]) == 1
    finally:
        initializer.db_handler.close()


def test_init_skips_non_local_source_types(config_path, config_snapshot_file, isolated_bootstrap):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({
        "sources": [{"type": "3rd-party", "path": "n/a"}]
    }))

    initializer = Initializer()
    try:
        initializer.init()

        contexts = initializer.db_handler.execute(Query("SELECT context_id FROM contexts"), commit=False)
        assert contexts == []
    finally:
        initializer.db_handler.close()


def test_initializer_can_be_constructed_before_config_file_exists(config_path, monkeypatch):
    monkeypatch.setattr("vcs.initialize.get_db_url", lambda: ":memory:")
    assert not config_path.exists()

    initializer = Initializer()
    initializer.db_handler.close()

    assert config_path.exists()
    assert initializer.sources == []


def _write_config(config_path, source_paths):
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(yaml.safe_dump({
        "sources": [{"type": "local", "path": str(p)} for p in source_paths]
    }))


def test_ac1_dropped_source_removes_mirror_content_after_a_later_boot(
    config_path, config_snapshot_file, isolated_bootstrap, tmp_path
):
    source_a = tmp_path / "source_a"
    source_a.mkdir()
    (source_a / "a.txt").write_text("from a")

    source_b = tmp_path / "source_b"
    source_b.mkdir()
    (source_b / "b.txt").write_text("from b")

    _write_config(config_path, [source_a, source_b])
    first_boot = Initializer()
    first_boot.init()
    first_boot.db_handler.close()

    # Offline edit: source_a dropped from config.yaml while no daemon ran.
    _write_config(config_path, [source_b])
    second_boot = Initializer()
    try:
        second_boot.init()

        repo_path, relpath = mirror_path.resolve_mirror_location(
            str(source_a / "a.txt"), [str(source_a)]
        )
        assert path_exists_at_rev(repo_path, relpath, "HEAD") is False

        rows = dict(second_boot.db_handler.execute(
            Query("SELECT location, status FROM locations"), commit=False
        ))
        assert rows[path_normalize(str(source_a / "a.txt"))] == 0
    finally:
        second_boot.db_handler.close()


def test_ac2_dropped_source_removal_commit_uses_startup_reconcile_actor(
    config_path, config_snapshot_file, isolated_bootstrap, tmp_path
):
    source_a = tmp_path / "source_a"
    source_a.mkdir()
    (source_a / "a.txt").write_text("from a")

    _write_config(config_path, [source_a])
    first_boot = Initializer()
    first_boot.init()
    first_boot.db_handler.close()

    _write_config(config_path, [])
    second_boot = Initializer()
    try:
        second_boot.init()

        repo_path, relpath = mirror_path.resolve_mirror_location(
            str(source_a / "a.txt"), [str(source_a)]
        )
        author = commit_info(repo_path, relpath).author
        assert "startup:reconcile" in author
    finally:
        second_boot.db_handler.close()


def test_ac3_unchanged_config_produces_no_spurious_commits_on_a_later_boot(
    config_path, config_snapshot_file, isolated_bootstrap, tmp_path
):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "doc.txt").write_text("hello world")

    _write_config(config_path, [source_dir])
    first_boot = Initializer()
    first_boot.init()
    first_boot.db_handler.close()

    repo_path, relpath = mirror_path.resolve_mirror_location(
        str(source_dir / "doc.txt"), [str(source_dir)]
    )
    count_after_first_boot = _log_count(repo_path)

    second_boot = Initializer()
    try:
        second_boot.init()

        assert _log_count(repo_path) == count_after_first_boot
    finally:
        second_boot.db_handler.close()


def test_ac5_config_snapshot_reflects_current_sources_after_a_later_boot(
    config_path, config_snapshot_file, isolated_bootstrap, tmp_path
):
    source_a = tmp_path / "source_a"
    source_a.mkdir()
    (source_a / "a.txt").write_text("from a")

    source_b = tmp_path / "source_b"
    source_b.mkdir()
    (source_b / "b.txt").write_text("from b")

    _write_config(config_path, [source_a, source_b])
    first_boot = Initializer()
    first_boot.init()
    first_boot.db_handler.close()

    _write_config(config_path, [source_b])
    second_boot = Initializer()
    try:
        second_boot.init()

        snapshot = yaml.safe_load(config_snapshot_file.read_text())
        assert [s["path"] for s in snapshot["sources"]] == [str(source_b).replace("\\", "/")]
    finally:
        second_boot.db_handler.close()


def _tree_paths(repo_path, rev):
    result = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", rev],
        cwd=str(repo_path), check=True, capture_output=True, text=True,
    )
    return set(result.stdout.strip().splitlines())


def _stage_without_committing(repo_path, relpath, content):
    blob = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=str(repo_path), input=content, check=True, capture_output=True,
    ).stdout.decode().strip()
    subprocess.run(
        ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob},{relpath}"],
        cwd=str(repo_path), check=True, capture_output=True,
    )


def test_ac6_boot_resets_a_stale_index_before_this_boots_own_writes(
    config_path, config_snapshot_file, isolated_bootstrap, tmp_path
):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "existing.txt").write_text("already tracked")
    (source_dir / "new.txt").write_text("added this boot")

    repo_path, relpath = mirror_path.resolve_mirror_location(
        str(source_dir / "existing.txt"), [str(source_dir)]
    )
    git_store.init_repo(repo_path)
    git_store.write(
        repo_path, relpath, b"already tracked",
        message="pre-seed", author="Test Author <test@chrono-ctx.local>",
    )
    _stage_without_committing(repo_path, "stale.txt", b"crash leftover")

    _write_config(config_path, [source_dir])
    initializer = Initializer()
    try:
        initializer.init()

        # existing.txt's own write() no-ops (content unchanged) - new.txt's
        # is the actually new commit, so its head_rev is the current HEAD.
        head = git_store.head_rev(repo_path, "new.txt")
        assert _tree_paths(repo_path, head) == {"existing.txt", "new.txt"}
    finally:
        initializer.db_handler.close()


def test_ec1_boot_does_not_fail_when_git_repo_dir_does_not_exist_yet(
    config_path, config_snapshot_file, isolated_bootstrap
):
    initializer = Initializer()
    try:
        initializer.init()
    finally:
        initializer.db_handler.close()
