from vcs.db.sqlite import DBHandler
from vcs.adapters.local_adapter import LocalAdapter
from vcs.shared.config import create_dirs

import vcs.services.configure as configure
import vcs.services.mirror_path as mirror_path
from vcs.services import git_store
from vcs.services.versioning import active_locations, reconcile_dropped_sources, sync_source_status
from vcs.services.db import init_db
from vcs.services.configure import derive_watch_targets, init_config_file, parse_config
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

        # Repair before anything this boot writes to a mirror repo (spec
        # 026's reconcile below, the backfill scan) - a prior force-kill can
        # leave a repo's index staged-but-uncommitted (spec 027); resetting
        # first means every later write this boot builds on a clean index.
        git_store.reset_stale_indexes(mirror_path.GIT_REPO_DIR)

        # Read before store_config_snapshot() below overwrites the baseline -
        # this is the watch-target set that was in effect for whatever the
        # DB currently thinks is active, needed to resolve mirror paths for
        # anything dropped from config.yaml while the daemon was offline
        # (docs/specs/026). [] on a genuinely first-ever boot (no snapshot
        # yet) - nothing can have been "dropped" from nothing.
        old_watch_targets = (
            derive_watch_targets(config=parse_config(from_snapshot=True))
            if configure.CONFIG_SNAPSHOT_FILE.exists() else []
        )
        previously_active = active_locations(self.db_handler)

        store_config_snapshot()
        #Fetch status with config sources
        sync_source_status(self.db_handler, sources=self.sources)

        still_active = set(active_locations(self.db_handler))
        dropped = [loc for loc in previously_active if loc not in still_active]
        reconcile_dropped_sources(self.db_handler, dropped, old_watch_targets)

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
    
    
    
    
    

    
