import yaml
import pytest

import vcs.adapters.local_adapter as local_adapter_module
import vcs.initialize as initialize_module
import vcs.services.db as db_module
import vcs.shared.config as shared_config
from vcs.initialize import Initializer
from vcs.shared.types import Query


@pytest.fixture
def isolated_bootstrap(tmp_path, config_snapshot_file, monkeypatch):
    """Redirects every real-filesystem side effect Initializer.init() touches
    (db file, blob dir, config snapshot dir) into tmp_path."""
    db_path = tmp_path / "db.sqlite"
    monkeypatch.setattr(initialize_module, "get_db_url", lambda: str(db_path))
    monkeypatch.setattr(db_module, "get_db_url", lambda: str(db_path))

    blob_dir = tmp_path / "blobs"
    monkeypatch.setattr(shared_config, "BLOB_DIR", blob_dir)
    monkeypatch.setattr(shared_config, "CONFIG_SNAPSHOT_DIR", config_snapshot_file.parent)
    monkeypatch.setattr(local_adapter_module, "BLOB_DIR", blob_dir)

    return {"db_path": db_path, "blob_dir": blob_dir}


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
        assert len(versions) == 1

        blob_dir = isolated_bootstrap["blob_dir"]
        assert blob_dir.exists()
        assert any(blob_dir.iterdir())

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
