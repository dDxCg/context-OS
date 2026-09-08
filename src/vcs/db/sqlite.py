from pathlib import Path
import sqlite3
from vcs.shared.types import Query

class DBHandler:
    def __init__(self, conn=None):
        self.conn = conn

    @classmethod
    def from_url(cls, db_url, timeout: float = 30.0):
        conn = sqlite3.connect(db_url, timeout=timeout)
        conn.execute("PRAGMA journal_mode=WAL")
        return cls(conn)
    
    def execute_script(self, script_url):
        if not self.conn:
            raise ValueError("No active database connection found.")
        
        script_path = Path(script_url)
        if not script_path.exists():
            raise FileNotFoundError(f"SQL script not found at: {script_url}")
        
        script = script_path.read_text()

        with self.conn:
            self.conn.executescript(script)
        
            
    def execute(self, query: Query, commit: bool = True):
        cursor = self.conn.cursor()
        if query.params:
            cursor.execute(query.query, query.params)
        else:
            cursor.execute(query.query)
        if commit:
            self.conn.commit()
        return cursor.fetchall()

    def check_connection(self):
        self.execute(commit=False, query=Query(query="SELECT 1"))
        return True

    def close(self):
        if self.conn:
            self.conn.close()
            self.conn = None

    def commit(self):
        if self.conn:
            self.conn.commit()

    def rollback(self):
        if self.conn:
            self.conn.rollback()

    def begin(self):
        self.execute(commit=False, query=Query(query="BEGIN"))

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()