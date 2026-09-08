
from pathlib import Path

from vcs.shared.config import NEW_VERSION_THRESHOLD
from utils.logger import log_enabled
from vcs.shared.types import CreatedEvent, ContextEntry, DeletedEvent, MovedEvent, Query, ModifiedEvent
from vcs.db.sqlite import DBHandler
from utils.helper import text_similarity, bytes_to_string, path_normalize, collect_files, get_path_stats
from vcs.shared.temp_file import TempFile
from vcs.services import git_store
from vcs.services.configure import derive_watch_targets, is_path_in_scope
from vcs.services.mirror_path import resolve_mirror_location, PathNotWatchedError

def _resolve_actor(event) -> tuple[str, str]:
    """(label, git-author-string) for event.actor, defaulting to the
    filesystem-origin label when unset (every real caller today, until MCP
    tool wiring / CLI capture actor - see docs/specs/007-actor-attribution.md)."""
    actor = getattr(event, "actor", None) or "unknown:filesystem"
    name, _, _ = actor.partition(":")
    return actor, f"{actor} <{name}@chrono-ctx.local>"


@log_enabled
def _append_context(db_handler: DBHandler, context_entry: ContextEntry, watch_targets: list[str], actor_label: str, author: str):
    location = context_entry.location
    loc_stats = get_path_stats(location)
    st_ino = loc_stats["st_ino"]
    st_dev = loc_stats["st_dev"]
    context_id = _check_existed_location(db_handler, location)

    try:
        db_handler.begin()
        if context_id is not None:
            _sync_location(db_handler, location, commit=False)
            _active_location_by_id(db_handler, context_id, commit=False)
        else:
            context_id = context_entry.context_id
            add_context = Query(
                query = "INSERT INTO contexts (context_id) VALUES (?)",
                params = (context_id,)
            )
            add_location = Query(
                query = """INSERT INTO
                        locations (st_ino, st_dev, context_id, location, provider)
                        VALUES (?, ?, ?, ?, ?)""",
                params = (st_ino, st_dev, context_id, context_entry.location, context_entry.provider)
            )

            db_handler.execute(commit=False, query=add_context)
            db_handler.execute(commit=False, query=add_location)
        db_handler.commit()
    except Exception:
        db_handler.rollback()
        raise

    repo_path, relpath = resolve_mirror_location(location, watch_targets)
    git_store.init_repo(repo_path)
    git_store.write(
        repo_path, relpath, Path(location).read_bytes(),
        message=f"created {relpath} via {actor_label}", author=author,
    )

@log_enabled
def modified_handle(db_handler: DBHandler, event: ModifiedEvent, tmp_file: TempFile, watch_targets: list[str] | None = None) -> bool:
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
    context_id = _get_context_id_by_location(db_handler, event.src)
    if context_id is None:
        create_event = CreatedEvent(src=event.src, actor=event.actor)
        return created_handle(db_handler, create_event, watch_targets=watch_targets)

    repo_path, relpath = resolve_mirror_location(event.src, watch_targets)
    git_store.init_repo(repo_path)

    new_content = tmp_file.read_bytes()
    current_rev = git_store.head_rev(repo_path, relpath)
    if current_rev is not None and not _should_commit(repo_path, relpath, new_content):
        tmp_file.delete_tmp_file()
        return

    actor_label, author = _resolve_actor(event)
    git_store.write(
        repo_path, relpath, new_content,
        message=f"modified {relpath} via {actor_label}", author=author,
    )
    tmp_file.delete_tmp_file()


def _should_commit(repo_path: Path, relpath: str, upcoming_bytes: bytes) -> bool:
    current_path = repo_path / relpath
    if not current_path.exists():
        return True
    current_bytes = current_path.read_bytes()
    similarity = text_similarity(bytes_to_string(current_bytes), bytes_to_string(upcoming_bytes))
    return similarity < NEW_VERSION_THRESHOLD


@log_enabled
def moved_handle(db_handler: DBHandler, event: MovedEvent, watch_targets: list[str] | None = None):
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
    active = 1 if is_path_in_scope(event.dst) else 0

    # Subtree rewrite first, pure SQL, no filesystem access - so a
    # directory move is immune to event.dst having already changed again
    # by the time the exact-node branch below tries to stat it.
    src_prefix = event.src.rstrip("/") + "/"
    dst_prefix = event.dst.rstrip("/") + "/"
    subtree_query = Query(
        query="""UPDATE locations
                    SET location = ? || substr(location, ?),
                        status = ?
                  WHERE substr(location, 1, ?) = ?""",
        params=(dst_prefix, len(src_prefix) + 1, active, len(src_prefix), src_prefix),
    )
    db_handler.execute(subtree_query, commit=True)

    # Exact-node update (single-file rename case). Guarded: a missing dst
    # (already moved/deleted again, or this event was for a directory with
    # no row of its own) degrades to a no-op instead of raising.
    try:
        loc_stats = get_path_stats(event.dst)
    except FileNotFoundError:
        loc_stats = None
    if loc_stats is not None:
        context_id = _get_context_id_by_location(db_handler, event.dst)
        update_path = Query(
            query="UPDATE locations SET location = ?, st_ino = ?, st_dev = ?, status = ? WHERE context_id = ?",
            params=(event.dst, loc_stats["st_ino"], loc_stats["st_dev"], active, context_id),
        )
        db_handler.execute(update_path, commit=True)

    actor_label, author = _resolve_actor(event)

    src_repo_path, src_relpath = resolve_mirror_location(event.src, watch_targets)
    git_store.init_repo(src_repo_path)

    try:
        dst_repo_path, dst_relpath = resolve_mirror_location(event.dst, watch_targets)
    except PathNotWatchedError:
        # dst isn't under any watch target - no mirror to write it into.
        # Recorded as a departure, the same way a delete is.
        git_store.remove(
            src_repo_path, src_relpath,
            message=f"moved out of scope: {src_relpath} to {event.dst} via {actor_label}",
            author=author,
        )
        return

    if src_repo_path == dst_repo_path:
        git_store.move(
            src_repo_path, src_relpath, dst_relpath,
            message=f"moved {src_relpath} to {dst_relpath} via {actor_label}", author=author,
        )
    else:
        # git mv can't span two repos - write the content into the
        # destination repo (event.dst already exists on disk, the OS move
        # already happened by the time this event fires) then remove it
        # from the source repo.
        git_store.init_repo(dst_repo_path)
        content = Path(event.dst).read_bytes()
        git_store.write(
            dst_repo_path, dst_relpath, content,
            message=f"moved in from {src_relpath} via {actor_label}", author=author,
        )
        git_store.remove(
            src_repo_path, src_relpath,
            message=f"moved out to {dst_relpath} via {actor_label}", author=author,
        )


@log_enabled
def created_handle(db_handler: DBHandler, event: CreatedEvent, watch_targets: list[str] | None = None):
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
    context_entry = ContextEntry.from_path(event.src)
    actor_label, author = _resolve_actor(event)
    _append_context(db_handler, context_entry, watch_targets, actor_label, author)

@log_enabled
def deleted_handle(db_handler: DBHandler, event: DeletedEvent, watch_targets: list[str] | None = None):
    watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
    prefix = event.src.rstrip("/") + "/"
    query = Query(
        query = "UPDATE locations SET status = 0 WHERE location = ? OR substr(location, 1, ?) = ?",
        params = (event.src, len(prefix), prefix)
    )
    db_handler.execute(commit=True, query=query)

    repo_path, relpath = resolve_mirror_location(event.src, watch_targets)
    git_store.init_repo(repo_path)
    actor_label, author = _resolve_actor(event)
    git_store.remove(repo_path, relpath, message=f"deleted {relpath} via {actor_label}", author=author)

@log_enabled
def sync_source_status(db_handler: DBHandler, sources):
    try:
        db_handler.begin()
        deactive_all = Query(
            query="UPDATE locations SET status = 0"
        ) 
        db_handler.execute(deactive_all, commit=False)
        for source in sources:
            source_path = path_normalize(source["path"])
            file_paths = collect_files(source_path)
            for path in file_paths:
                loc_stats = get_path_stats(path)
                st_ino = loc_stats["st_ino"]
                st_dev = loc_stats["st_dev"]
                sync_query = Query(
                    query = """UPDATE locations 
                            SET location = ?, status = 1 
                            WHERE st_ino = ? AND st_dev = ?""",
                    params = (path, st_ino, st_dev)
                )
                db_handler.execute(sync_query, commit=False)
        db_handler.commit()
    except Exception:
        db_handler.rollback()
        raise
    

def _get_context_id_by_location(db_handler: DBHandler, location: str):
    loc_stats = get_path_stats(location)
    st_ino = loc_stats["st_ino"]
    st_dev = loc_stats["st_dev"]
    get_context_id = Query(
        query = "SELECT context_id FROM locations WHERE st_ino = ? AND st_dev = ?",
        params = (st_ino, st_dev)
    )
    res = db_handler.execute(commit=False, query=get_context_id)
    return res[0][0] if res else None


def _check_existed_location(db_handler: DBHandler, location: str):
    loc_stats = get_path_stats(location)
    st_ino = loc_stats["st_ino"]
    st_dev = loc_stats["st_dev"]
    check_path = Query(
        query="SELECT context_id FROM locations WHERE st_ino = ? AND st_dev = ?",
        params = (st_ino, st_dev)
    )
    res = db_handler.execute(commit=False, query = check_path)
    if res:
        return res[0][0]
    return None
    

def _active_location_by_id(db_handler: DBHandler, context_id, commit=False):
    active_query = Query(
        query="UPDATE locations SET status = 1 WHERE context_id=?",
        params=(context_id,)
    )
    db_handler.execute(active_query, commit)

def _sync_location(db_handler: DBHandler, location: str, commit=False):
    loc_stats = get_path_stats(location)
    st_ino = loc_stats["st_ino"]
    st_dev = loc_stats["st_dev"]
    sync_location_query = Query(
        query="UPDATE locations SET location = ? WHERE st_ino = ? AND st_dev = ?",
        params = (location, st_ino, st_dev)
    )
    db_handler.execute(sync_location_query, commit=commit)

