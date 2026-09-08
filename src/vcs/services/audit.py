from vcs.db.sqlite import DBHandler
from vcs.services import git_store
from vcs.services.configure import derive_watch_targets, is_path_in_scope
from vcs.services.mirror_path import PathNotWatchedError, resolve_mirror_location
from vcs.services.versioning import current_version
from vcs.shared.types import Query


class OutOfScopeError(PermissionError):
    """path is not in the current config source scope.

    Fail-closed, no elicitation - matches the read-only policy
    docs/specs/audit-read-api.md specifies for its future HTTP layer. Unlike
    the MCP guardrail, there is no path to silently regain access here.
    """


def _check_scope(path: str) -> None:
    if not is_path_in_scope(path):
        raise OutOfScopeError(path)


def get_sources(db_handler: DBHandler, watch_targets: list[str] | None = None) -> list[dict]:
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
    rows = db_handler.execute(
        Query("SELECT location, provider, status FROM locations"), commit=False
    )
    return [
        {
            "location": location,
            "provider": provider,
            "status": status,
            "version": current_version(location, watch_targets),
        }
        for location, provider, status in rows
    ]


def get_version_list(path: str, watch_targets: list[str] | None = None) -> list[dict]:
    _check_scope(path)
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
    try:
        repo_path, relpath = resolve_mirror_location(path, watch_targets)
    except PathNotWatchedError:
        return []
    git_store.init_repo(repo_path)
    return git_store.log_history(repo_path, relpath)


def check_diff(path: str, v1: str, v2: str, watch_targets: list[str] | None = None) -> str:
    _check_scope(path)
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
    try:
        repo_path, relpath = resolve_mirror_location(path, watch_targets)
    except PathNotWatchedError:
        return ""
    git_store.init_repo(repo_path)
    if git_store.head_rev(repo_path, relpath) is None:
        return ""
    return git_store.diff(repo_path, relpath, v1, v2)


def rollback_source(path, version: int):
    pass
