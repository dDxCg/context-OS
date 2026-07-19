import pytest


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Points CONFIG_PATH at an isolated, not-yet-existing file under tmp_path."""
    path = tmp_path / "config.yaml"
    monkeypatch.setenv("CONFIG_PATH", str(path))
    return path


@pytest.fixture
def config_snapshot_file(tmp_path, monkeypatch):
    """Redirects vcs.services.configure's snapshot file into tmp_path."""
    import vcs.services.configure as configure

    path = tmp_path / "snapshot" / "config.yaml"
    monkeypatch.setattr(configure, "CONFIG_SNAPSHOT_FILE", path)
    return path
