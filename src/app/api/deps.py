import hmac
import sqlite3

from fastapi import HTTPException, Security
from fastapi.security import APIKeyHeader

from vcs.db.sqlite import DBHandler
from utils.helper import get_db_url, get_http_api_key

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(x_api_key: str | None = Security(_api_key_header)) -> None:
    """Router-level dependency: rejects any request without the exact
    configured HTTP_API_KEY. An unset/empty key fails closed - there is no
    way to run the HTTP API unauthenticated by leaving it unconfigured."""
    configured_key = get_http_api_key()
    if not configured_key or not x_api_key or not hmac.compare_digest(x_api_key, configured_key):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def get_db_handler():
    """FastAPI dependency: yields a DBHandler, closes it after the request.

    Bypasses DBHandler.from_url() to pass check_same_thread=False - FastAPI
    runs sync dependencies and sync path functions via run_in_threadpool,
    each call free to land on a different worker thread, and sqlite3's
    default same-thread check rejects a connection used across that
    boundary. Safe here because the connection is request-scoped: created
    fresh per request and never shared across concurrent requests.
    """
    conn = sqlite3.connect(get_db_url(), check_same_thread=False)
    db_handler = DBHandler(conn)
    try:
        yield db_handler
    finally:
        db_handler.close()
