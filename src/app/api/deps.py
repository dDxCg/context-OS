import sqlite3

from vcs.db.sqlite import DBHandler
from utils.helper import get_db_url


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
