import os
from pathlib import Path

from utils.helper import anchored

# Anchored to the project root rather than cwd: the MCP server and the VCS
# runtime are separate processes whose working directories need not match.
# Env-overridable like DATABASE_URL/SCHEMA_PATH, so a second instance can be
# pointed at its own data directory.
SNAPSHOT_DIR = Path(anchored(os.getenv("SNAPSHOT_DIR", "data/snapshots")))
BLOB_DIR = SNAPSHOT_DIR / "blobs"
GIT_REPO_DIR = Path(anchored(os.getenv("GIT_REPO_DIR", "data/repo")))
CONFIG_SNAPSHOT_DIR = SNAPSHOT_DIR / "configs"
CONFIG_SNAPSHOT_FILE = CONFIG_SNAPSHOT_DIR / "config.yaml"
NEW_VERSION_THRESHOLD = 0.9

def create_dirs():
    BLOB_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

if __name__ == "__main__":
    create_dirs()
