from fastmcp import Context
from fastmcp.server.elicitation import AcceptedElicitation

from vcs.services.configure import add_sources, is_path_in_scope


async def ensure_scope(ctx: Context, path: str) -> bool:
    """Synchronous-per-call guardrail: check `path` against the current
    config.yaml source scope. If out of scope, ask the client to approve
    adding it. Fails closed (blocks) on decline, cancel, or a client that
    doesn't support elicitation."""
    if is_path_in_scope(path):
        return True

    try:
        result = await ctx.elicit(
            f"'{path}' is outside the configured source scope. Approve adding it?",
            response_type=bool,
        )
    except Exception:
        return False

    if isinstance(result, AcceptedElicitation) and result.data:
        add_sources([path])
        return True

    return False
