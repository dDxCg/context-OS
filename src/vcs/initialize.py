from vcs.db.sqlite import DBHandler
from vcs.adapters.local_adapter import LocalAdapter
from vcs.shared.config import create_dirs

from vcs.services.versioning import sync_source_status
from vcs.services.db import init_db
from vcs.services.configure import init_config_file
from vcs.services.configure import store_config_snapshot

from utils.helper import get_db_url, get_config_path
from utils.logger import log_enabled

from pathlib import Path
import yaml

class Initializer:
    def __init__(self):
        self.db_handler = DBHandler.from_url(get_db_url())
        init_config_file()
        self.sources = self._get_sources(get_config_path())


    @log_enabled
    def init(self):
        create_dirs()

        self._init_schema()

        store_config_snapshot()
        #Fetch status with config sources
        sync_source_status(self.db_handler, sources=self.sources)

        for source in self.sources:
            if source["type"] == "local":
                adapter = LocalAdapter(self.db_handler)
                adapter.local_processing(source["path"])

    def _init_schema(self):
        init_db(self.db_handler)

    def _get_sources(self, config_path: Path):
        with open(config_path, "r") as f:
            config = yaml.safe_load(f) or {"sources": []}
        return config["sources"]
    
    
    
    
    

    
