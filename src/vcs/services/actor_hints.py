import time

from utils.helper import path_normalize
from vcs.db.sqlite import DBHandler
from vcs.shared.types import Query

DEFAULT_TTL_SECONDS = 5.0


def set_hint(db_handler: DBHandler, location: str, actor: str, ttl_seconds: float = DEFAULT_TTL_SECONDS) -> None:
    """Record that the next filesystem event for `location` should be
    attributed to `actor`. Overwrites any existing hint for the location."""
    normalized = path_normalize(location)
    expires_at = time.time() + ttl_seconds
    db_handler.execute(Query(
        "INSERT OR REPLACE INTO pending_actor_hints (location, actor, expires_at) VALUES (?, ?, ?)",
        (normalized, actor, expires_at),
    ))


def consume_hint(db_handler: DBHandler, location: str) -> str | None:
    """Pop (read then delete) the pending actor hint for `location`. None
    if there isn't one, or it already expired - an expired row is deleted
    here too, so it can never attach to a later, unrelated edit."""
    normalized = path_normalize(location)
    rows = db_handler.execute(
        Query("SELECT actor, expires_at FROM pending_actor_hints WHERE location = ?", (normalized,)),
        commit=False,
    )
    db_handler.execute(
        Query("DELETE FROM pending_actor_hints WHERE location = ?", (normalized,))
    )
    if not rows:
        return None
    actor, expires_at = rows[0]
    if time.time() > expires_at:
        return None
    return actor
