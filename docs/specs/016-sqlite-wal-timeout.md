# 016 — SQLite WAL mode + busy timeout on `DBHandler.from_url`

Status: implemented

## Context

[issues.md #20](../agents/issues.md): `DBHandler.from_url` opens a plain
`sqlite3.connect(db_url)` with no `PRAGMA journal_mode=WAL` and no explicit
busy timeout. In SQLite's default rollback-journal mode a writer blocks all
readers. Not a problem while only one process ever opens the database — it
becomes one the moment a CLI command reads the DB while the daemon is
writing (`ctx history` failing with `database is locked`, or `ctx rollback`
failing against a busy daemon).

## Scope

**In**
- `DBHandler.from_url`: set `PRAGMA journal_mode=WAL` on the connection, and
  pass an explicit `timeout` (seconds) to `sqlite3.connect`, defaulting to
  30.0 and overridable via a keyword argument.
- `DBHandler`: `__enter__`/`__exit__` so a short-lived CLI call can use
  `with DBHandler.from_url(...) as db:` and always close the connection,
  even on an exception mid-command.

**Out**
- `execute()`'s `self.conn is None` guard, `execute_script`'s error
  shape, and `execute()`'s `commit=True` default — real sharp edges noted
  alongside #20 in issues.md, but a separate behavior change each; not
  touched here.
- Wiring CLI commands to actually use the new context-manager form — that's
  a call-site change across `app/cli/app.py`, out of scope for this spec
  (they already construct/discard a `DBHandler` per command today, which
  still works, just doesn't close explicitly).

## Acceptance criteria

- AC-1. `DBHandler.from_url(db_url)` (file-backed, not `:memory:`) opens a
  connection with `journal_mode` set to `wal`.
- AC-2. `DBHandler.from_url(db_url, timeout=N)` passes `N` through as the
  connection's busy timeout (`PRAGMA busy_timeout` reads back `N * 1000`
  milliseconds). Omitting `timeout` defaults to 30.0s.
- AC-3. `with DBHandler.from_url(db_url) as db: ...` closes the connection
  on normal exit — `db.conn is None` after the block.
- AC-4. The same context-manager form closes the connection when the body
  raises — `db.conn is None` after the exception propagates.

## Error cases

None new — `__exit__` always closes and re-raises, never swallows.

## Contracts

```python
# vcs/db/sqlite.py
class DBHandler:
    @classmethod
    def from_url(cls, db_url: str, timeout: float = 30.0) -> "DBHandler": ...
    def __enter__(self) -> "DBHandler": ...
    def __exit__(self, exc_type, exc, tb) -> None: ...
```

## Non-goals / open questions

None outstanding.
