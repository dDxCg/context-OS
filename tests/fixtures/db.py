from pathlib import Path
import sqlite3
import pytest
from vcs.db.sqlite import DBHandler
from tests.fixtures.seed import Seeder

@pytest.fixture
def db_handler():
    conn = sqlite3.connect(":memory:")

    schema = Path(
        "src/vcs/db/schema.sql"
    ).read_text()

    conn.executescript(schema)

    yield DBHandler(conn=conn)

    conn.close()

@pytest.fixture
def seeder(db_handler):
    return Seeder(db_handler)


