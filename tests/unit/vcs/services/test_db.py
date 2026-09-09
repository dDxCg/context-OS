import sqlite3

from vcs.db.sqlite import DBHandler
from vcs.services.db import init_db
from vcs.shared.types import Query


def test_ac3_init_db_applies_schema_regardless_of_cwd(monkeypatch, tmp_path):
    """The actual bug (spec 030): init_db() used to build its own raw,
    un-anchored 'data/schema.sql' path instead of calling get_schema_path()
    - it only ever worked because every real invocation happened to run
    from the repo root. Running from an unrelated cwd is what catches it."""
    monkeypatch.chdir(tmp_path)

    db_handler = DBHandler(conn=sqlite3.connect(":memory:"))
    try:
        init_db(db_handler)

        tables = db_handler.execute(
            Query("SELECT name FROM sqlite_master WHERE type = 'table'"), commit=False
        )
        table_names = {row[0] for row in tables}
        assert "contexts" in table_names
        assert "locations" in table_names
    finally:
        db_handler.close()
