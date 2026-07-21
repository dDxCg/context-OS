from fastmcp import Context
from fastmcp.server.elicitation import AcceptedElicitation

from vcs.services.configure import add_sources, is_path_in_scope


class ScopeGrant:
    """Result of a scope check. Truthy when the caller may proceed.

    Persisting an approval is deliberately split from checking it. The check
    is the security boundary and must happen before the operation; the write
    to config.yaml must happen after, because a granted path is not merely an
    access record - it becomes a versioned source and a watch target. Writing
    it up front meant a failed operation still permanently widened scope.

    `commit()` is a no-op unless the path was newly approved, so callers can
    always call it on success without caring which case they got.
    """

    __slots__ = ("allowed", "_pending_path")

    def __init__(self, allowed: bool, pending_path: str | None = None):
        self.allowed = allowed
        self._pending_path = pending_path

    def __bool__(self) -> bool:
        return self.allowed

    def commit(self) -> None:
        if self._pending_path is None:
            return
        add_sources([self._pending_path])
        self._pending_path = None


async def ensure_scope(ctx: Context, path: str) -> ScopeGrant:
    """Synchronous-per-call guardrail: check `path` against the current
    config.yaml source scope. If out of scope, ask the client to approve
    adding it. Fails closed (blocks) on decline, cancel, or a client that
    doesn't support elicitation.

    On approval the grant is returned *pending* - the caller must call
    commit() once its operation succeeds.
    """
    if is_path_in_scope(path):
        return ScopeGrant(True)

    try:
        result = await ctx.elicit(
            f"'{path}' is outside the configured source scope. Approve adding it?",
            response_type=bool,
        )
    except Exception:
        return ScopeGrant(False)

    if isinstance(result, AcceptedElicitation) and result.data:
        return ScopeGrant(True, pending_path=path)

    return ScopeGrant(False)
