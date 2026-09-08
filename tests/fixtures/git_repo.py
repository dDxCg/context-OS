import pytest


@pytest.fixture
def repo_path(tmp_path):
    """Path for a mirror repo that does not exist on disk yet."""
    return tmp_path / "repo"


@pytest.fixture
def initialized_repo(repo_path):
    """A repo_path already taken through init_repo()."""
    from vcs.services import git_store

    git_store.init_repo(repo_path)
    return repo_path
