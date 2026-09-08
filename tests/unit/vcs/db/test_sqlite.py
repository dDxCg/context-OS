from vcs.db.sqlite import DBHandler
from vcs.shared.types import Query


def test_ac1_from_url_enables_wal_journal_mode(tmp_path):
    db_url = str(tmp_path / "test.sqlite")

    db = DBHandler.from_url(db_url)

    mode = db.execute(commit=False, query=Query(query="PRAGMA journal_mode"))[0][0]
    assert mode.lower() == "wal"
    db.close()


def test_ac2_from_url_default_timeout_is_30s(tmp_path):
    db_url = str(tmp_path / "test.sqlite")

    db = DBHandler.from_url(db_url)

    busy_timeout_ms = db.execute(commit=False, query=Query(query="PRAGMA busy_timeout"))[0][0]
    assert busy_timeout_ms == 30_000
    db.close()


def test_ac2_from_url_accepts_explicit_timeout(tmp_path):
    db_url = str(tmp_path / "test.sqlite")

    db = DBHandler.from_url(db_url, timeout=5.0)

    busy_timeout_ms = db.execute(commit=False, query=Query(query="PRAGMA busy_timeout"))[0][0]
    assert busy_timeout_ms == 5_000
    db.close()


def test_ac3_context_manager_closes_connection_on_normal_exit(tmp_path):
    db_url = str(tmp_path / "test.sqlite")

    with DBHandler.from_url(db_url) as db:
        pass

    assert db.conn is None


def test_ac4_context_manager_closes_connection_on_exception(tmp_path):
    db_url = str(tmp_path / "test.sqlite")
    db = None

    try:
        with DBHandler.from_url(db_url) as handler:
            db = handler
            raise ValueError("boom")
    except ValueError:
        pass

    assert db.conn is None
