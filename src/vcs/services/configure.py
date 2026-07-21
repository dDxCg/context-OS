from pathlib import Path
from utils.helper import get_config_path, path_normalize
from vcs.shared.config import CONFIG_SNAPSHOT_FILE

import yaml


def health_check():
    pass

def init_config_file():
    config_path = Path(get_config_path())
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.touch(exist_ok=True)

def reset_config_file():
    config_path = Path(get_config_path())
    if config_path.is_file():
        config_path.unlink()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.touch(exist_ok=True)

def add_sources(paths: list[Path]):
    config = parse_config()
    path_set = set(paths)
    for path in path_set:
        # Skip anything an existing source already covers, not just exact
        # duplicates - otherwise config.yaml accumulates entries subsumed by a
        # broader source (the MCP guardrail grants one path at a time).
        if is_path_in_scope(path, config=config):
            continue
        source_entry = {
            "type": "local",
            "path": path_normalize(path)
        }
        config["sources"].append(source_entry)
        
    _dump_config(config_content=config)


def remove_sources(paths: list[Path]):
    config = parse_config()
    paths = [path_normalize(path) for path in paths]

    path_set = set(paths)

    config["sources"] = [item for item in config["sources"] if item["path"] not in path_set]
    _dump_config(config_content=config)
    

def is_path_in_scope(path, config=None) -> bool:
    config = config or parse_config()
    target = Path(path_normalize(path))
    for source in config["sources"]:
        if source.get("type") != "local":
            continue
        source_path = Path(source["path"])
        if target == source_path or target.is_relative_to(source_path):
            return True
    return False

def derive_watch_targets(config=None) -> list[str]:
    """Minimal set of directories covering every local source.

    Sources are file-granular - the MCP guardrail grants the narrowest path it
    can - but watchdog watches directories. Map each source to its containing
    directory, then drop any directory an ancestor already covers recursively,
    so N approved files in one directory collapse to a single watch.

    Callers must filter incoming events against is_path_in_scope(): a watch
    derived this way is deliberately broader than the granted scope.
    """
    config = config or parse_config()

    dirs = set()
    for source in config["sources"]:
        if source.get("type") != "local":
            continue
        target = _nearest_existing_dir(source["path"])
        if target is not None:
            dirs.add(target)

    return [
        d for d in dirs
        if not any(o != d and Path(d).is_relative_to(Path(o)) for o in dirs)
    ]


def _nearest_existing_dir(path) -> str | None:
    """Closest existing directory at or above `path`, or None.

    Returns None rather than walking all the way up to a filesystem root - a
    source whose entire ancestry is missing is not watchable, and watching '/'
    or 'C:\\' to compensate would put the whole disk under the watcher.
    """
    p = Path(path_normalize(path))
    if p.is_dir():
        return path_normalize(str(p))

    p = p.parent
    while not p.is_dir():
        if p == p.parent:
            return None
        p = p.parent

    # Reaching an anchor means nothing along the path existed.
    if p == Path(p.anchor):
        return None
    return path_normalize(str(p))


def get_config_diff(config=None):
    """Sources added/removed since the last snapshot.

    Pass `config` to diff against a config you already read. Callers that
    afterwards call store_config_snapshot() must do this and pass the same
    object to both: config.yaml can be rewritten between the two calls (the
    MCP guardrail appends one path per approval), and re-reading it in the
    snapshot would advance the baseline past changes never applied - losing
    them permanently, since the next diff would then report nothing.
    """
    curr = config or parse_config()
    prev = parse_config(from_snapshot=True)
    curr_paths = [s["path"] for s in curr["sources"]]
    prev_paths = [s["path"] for s in prev["sources"]]
    added_ls = [path for path in curr_paths if path not in prev_paths]
    deleted_ls = [path for path in prev_paths if path not in curr_paths]
    return {
        "added": added_ls,
        "deleted": deleted_ls
    }

def parse_config(from_snapshot=False):
    config_path = get_config_path()
    if from_snapshot:
        config_path = CONFIG_SNAPSHOT_FILE
    with open(config_path) as f:
        config_content = yaml.safe_load(f) or {"sources": []}

    for source in config_content["sources"]:
        source["path"] = path_normalize(source["path"])

    config_content["sources"] = _sources_dedup(config_content["sources"])

    return config_content

def store_config_snapshot(config_content=None):
    """Record the current source list as the diff baseline.

    Pass the same config object that get_config_diff() was given, so the
    baseline only ever advances to state that was actually applied.
    """
    config_content = config_content or parse_config()
    if not CONFIG_SNAPSHOT_FILE.exists():
        CONFIG_SNAPSHOT_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_SNAPSHOT_FILE.touch(exist_ok=True)
    with open(CONFIG_SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            config_content,
            f,
            sort_keys=False,      
            default_flow_style=False
        )

def recover_config():
    config_content = parse_config(from_snapshot=True)
    config_path = get_config_path()
    Path(config_path).parent.mkdir(parents=True, exist_ok=True)
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            config_content,
            f,
            sort_keys=False,      
            default_flow_style=False
        )


def _check_existed_path(path, config_content):
    for source in config_content["sources"]:
        if path_normalize(path) == path_normalize(source["path"]):
            return True
    return False        

def _dump_config(config_content):
    config_path = get_config_path()
    with open(config_path, "w", encoding="utf-8") as f:
        yaml.safe_dump(
            config_content,
            f,
            sort_keys=False,      
            default_flow_style=False
        )

def _sources_dedup(sources):
    return list({s["path"]: s for s in sources}.values())


if __name__ == "__main__":
    paths = [
        "knowledge.example\\skills",
        "knowledge.example\\tests"
    ]
    add_sources(paths)