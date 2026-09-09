from pathlib import Path

from utils.helper import path_normalize
from vcs.db.sqlite import DBHandler
from vcs.services import git_store
from vcs.services.configure import derive_watch_targets, is_path_in_scope
from vcs.services.mirror_path import PathNotWatchedError, repo_path_for, resolve_mirror_location
from vcs.services.versioning import current_version
from vcs.shared.types import Query

_SESSION_ROLLBACK_ACTOR = "cli:rollback-session"
_UNATTRIBUTABLE_ACTOR = "unknown:filesystem"


class OutOfScopeError(PermissionError):
    """path is not in the current config source scope.

    Fail-closed, no elicitation - matches the read-only policy
    docs/specs/audit-read-api.md specifies for its future HTTP layer. Unlike
    the MCP guardrail, there is no path to silently regain access here.
    """


class InvalidSessionActorError(ValueError):
    """actor_label is too broad to roll back as a session - e.g. the
    generic unknown:filesystem fallback shared by every untracked edit."""


def _check_scope(path: str) -> None:
    if not is_path_in_scope(path):
        raise OutOfScopeError(path)


def _source_path_for(watch_target: str, relpath: str) -> str:
    normalized = path_normalize(watch_target)
    return normalized if not relpath else f"{normalized.rstrip('/')}/{relpath}"


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


def rollback_source(path: str, rev: str, watch_targets: list[str] | None = None) -> str:
    _check_scope(path)
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
    repo_path, relpath = resolve_mirror_location(path, watch_targets)
    git_store.init_repo(repo_path)

    expected_rev = git_store.head_rev(repo_path, relpath)
    content = git_store.show(repo_path, relpath, rev)

    actor_label = "cli:rollback"
    new_rev = git_store.write_with_check(
        repo_path, relpath, content,
        message=f"rollback {relpath} to {rev} via {actor_label}",
        author=f"{actor_label} <cli@chrono-ctx.local>",
        expected_rev=expected_rev,
    )

    Path(path).write_bytes(content)
    return new_rev


def rollback_session(actor_label: str, watch_targets: list[str] | None = None) -> dict:
    """Undo every change actor_label made, across every watch target.

    For each path the actor touched, restores the content to what it was
    immediately before the actor's *earliest* commit on that path (deletes
    it if the actor created it). Best-effort, not atomic across repos: each
    path's own restore is guarded by an optimistic-concurrency check against
    the actor's own last known commit on that path, so a real edit by
    someone else after the session is refused rather than clobbered - that
    path is reported failed, everything else still proceeds.
    """
    if actor_label == _UNATTRIBUTABLE_ACTOR:
        raise InvalidSessionActorError(
            f"{_UNATTRIBUTABLE_ACTOR!r} is the shared fallback for every "
            "untracked edit, not a session - refusing to roll back everything it matches"
        )
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()

    author = f"{_SESSION_ROLLBACK_ACTOR} <cli@chrono-ctx.local>"
    rolled_back = []
    failed = []

    for watch_target in watch_targets:
        repo_path = repo_path_for(watch_target)
        git_store.init_repo(repo_path)
        commits = git_store.commits_by_author(repo_path, actor_label)

        earliest_by_path: dict[str, dict] = {}
        latest_by_path: dict[str, dict] = {}
        for commit in commits:
            for relpath in commit["paths"]:
                earliest_by_path.setdefault(relpath, commit)
                latest_by_path[relpath] = commit

        for relpath, earliest in earliest_by_path.items():
            source_path = _source_path_for(watch_target, relpath)
            try:
                _check_scope(source_path)

                expected_rev = latest_by_path[relpath]["rev"]
                current_rev = git_store.head_rev(repo_path, relpath)
                if current_rev != expected_rev:
                    info = git_store.commit_info(repo_path, relpath)
                    raise git_store.ConcurrentEditError(
                        relpath, expected_rev, current_rev,
                        info.author if info else "", info.timestamp if info else "",
                    )

                actor_created_path = earliest["parent"] is None or not git_store.path_exists_at_rev(
                    repo_path, relpath, earliest["parent"]
                )
                if actor_created_path:
                    git_store.remove(
                        repo_path, relpath,
                        message=f"rollback session {actor_label}: undo create of {relpath}",
                        author=author,
                    )
                    Path(source_path).unlink(missing_ok=True)
                else:
                    content = git_store.show(repo_path, relpath, earliest["parent"])
                    git_store.write_with_check(
                        repo_path, relpath, content,
                        message=f"rollback session {actor_label}: revert {relpath} to {earliest['parent']}",
                        author=author,
                        expected_rev=current_rev,
                    )
                    Path(source_path).write_bytes(content)
                rolled_back.append(relpath)
            except (OutOfScopeError, git_store.ConcurrentEditError) as exc:
                failed.append({"path": relpath, "error": str(exc)})

    return {"rolled_back": rolled_back, "failed": failed}
