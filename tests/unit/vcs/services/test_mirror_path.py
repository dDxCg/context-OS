import pytest

from vcs.services import mirror_path
from vcs.shared.config import GIT_REPO_DIR


def test_ac1_file_directly_under_watch_target():
    repo_path, relpath = mirror_path.resolve_mirror_location(
        "C:/src/a.txt", ["C:/src"]
    )

    assert repo_path == GIT_REPO_DIR / "C/src"
    assert relpath == "a.txt"


def test_ac2_file_in_nested_subdirectory():
    repo_path, relpath = mirror_path.resolve_mirror_location(
        "C:/src/sub/b.txt", ["C:/src"]
    )

    assert repo_path == GIT_REPO_DIR / "C/src"
    assert relpath == "sub/b.txt"


def test_ac3_two_watch_targets_on_different_drives_do_not_collide():
    repo_path_a, _ = mirror_path.resolve_mirror_location("C:/a/x.txt", ["C:/a", "D:/b"])
    repo_path_d, relpath_d = mirror_path.resolve_mirror_location("D:/b/x.txt", ["C:/a", "D:/b"])

    assert repo_path_d != repo_path_a
    assert repo_path_d == GIT_REPO_DIR / "D/b"
    assert relpath_d == "x.txt"


def test_ac4_source_path_equals_watch_target_itself():
    repo_path, relpath = mirror_path.resolve_mirror_location("C:/src", ["C:/src"])

    assert repo_path == GIT_REPO_DIR / "C/src"
    assert relpath == ""


def test_ec1_source_path_not_under_any_watch_target_raises():
    with pytest.raises(mirror_path.PathNotWatchedError):
        mirror_path.resolve_mirror_location("C:/elsewhere/x.txt", ["C:/src"])


def test_repo_path_for_matches_resolve_mirror_location():
    expected, _ = mirror_path.resolve_mirror_location("C:/src", ["C:/src"])

    assert mirror_path.repo_path_for("C:/src") == expected
