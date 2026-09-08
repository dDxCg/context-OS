from pathlib import Path

from utils.helper import path_normalize
from vcs.shared.config import GIT_REPO_DIR


class PathNotWatchedError(ValueError):
    """source_path is not under any of the given watch_targets."""


def repo_dir_name(watch_target: str) -> str:
    """Deterministic, filesystem-safe directory name for one watch target."""
    normalized = path_normalize(watch_target)
    return normalized.replace(":", "")


def resolve_mirror_location(source_path: str, watch_targets: list[str]) -> tuple[Path, str]:
    """(repo_path under GIT_REPO_DIR, relpath within that repo) for
    source_path, given the current set of watch-target directories."""
    normalized = path_normalize(source_path)
    for target in watch_targets:
        target_norm = path_normalize(target)
        if normalized == target_norm:
            return GIT_REPO_DIR / repo_dir_name(target_norm), ""
        if normalized.startswith(target_norm.rstrip("/") + "/"):
            repo_path = GIT_REPO_DIR / repo_dir_name(target_norm)
            relpath = normalized[len(target_norm.rstrip("/")) + 1:]
            return repo_path, relpath
    raise PathNotWatchedError(f"{source_path} is not under any watch target")
