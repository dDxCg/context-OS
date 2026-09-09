from pathlib import Path

from utils.helper import get_db_url, get_schema_path
from vcs.db.sqlite import DBHandler

def init_db(db_handler: DBHandler):
    db_path = Path(get_db_url())
    if not db_path.exists():
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_path.touch(exist_ok=True)

    db_handler.execute_script(get_schema_path())

def reset_db(db_handler: DBHandler):
    db_path = Path(get_db_url())
    if db_path.is_file():
        db_path.unlink()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch(exist_ok=True)
    init_db(db_handler)
    

    

    
    