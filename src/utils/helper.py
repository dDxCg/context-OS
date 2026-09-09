import hashlib
import os
import re
import sys
from importlib import resources
from ulid import ULID
from dotenv import load_dotenv
from pathlib import Path, PureWindowsPath

DEFAULT_ENCODING = "utf-8"

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")


def _is_source_checkout(candidate: Path) -> bool:
    """Whether candidate looks like a real chrono-ctx source checkout
    (uv sync / pip install -e .), not a packaged install's site-packages.
    schema.sql itself (spec 030) moved under src/vcs/db/ - resolved via
    importlib.resources now, not usable as a PROJECT_ROOT marker anymore."""
    return (candidate / "pyproject.toml").is_file() or (candidate / "src" / "vcs" / "db" / "schema.sql").is_file()


def _default_user_data_dir() -> Path:
    """OS-appropriate per-user data directory, hand-rolled rather than a
    new `platformdirs` dependency - three sys.platform branches doesn't
    justify one, and this project already branches on sys.platform
    directly elsewhere (daemon.py's POSIX/Windows split)."""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "chrono-ctx"


def _resolve_project_root(editable_candidate: Path) -> Path:
    """PROJECT_ROOT resolution order (spec 029):

    1. CHRONO_CTX_HOME env var, if set - explicit override, also how two
       independent chrono-ctx projects on one machine stay separate (no
       automatic namespacing is attempted).
    2. editable_candidate itself, if it looks like a real source checkout
       - unchanged behavior for `uv sync`/`pip install -e .`.
    3. An OS-appropriate per-user data directory - the packaged-install
       (`pip install chrono-ctx`) case, where editable_candidate resolves
       to somewhere inside site-packages with no project markers at all.
    """
    override = os.getenv("CHRONO_CTX_HOME")
    if override:
        return Path(override).expanduser().resolve()
    if _is_source_checkout(editable_candidate):
        return editable_candidate
    return _default_user_data_dir()


def _default_mode(editable_candidate: Path) -> str:
    """MODE's default when unset (spec 031): "dev" for a real source
    checkout (unchanged behavior for every contributor/CI flow), "prod"
    for a packaged install - a real pip-installed end-user run is never
    chrono-ctx-the-project's own dev mode, it just defaulted to that
    because a source checkout used to be the only way to run this at
    all."""
    return "dev" if _is_source_checkout(editable_candidate) else "prod"


# src/utils/helper.py -> src/utils -> src -> repo root, in a source
# checkout; a packaged install falls back per _resolve_project_root().
_EDITABLE_CANDIDATE = Path(__file__).resolve().parents[2]
PROJECT_ROOT = _resolve_project_root(_EDITABLE_CANDIDATE)
_DEFAULT_MODE = _default_mode(_EDITABLE_CANDIDATE)


def anchored(value: str) -> str:
    """Resolve a relative configured path against the project root, not cwd.

    config.yaml is the only coupling between the MCP server and the VCS
    runtime, and the MCP server runs over stdio - its cwd is chosen by
    whatever client spawns it. Resolving against cwd lets the two processes
    silently use different files: MCP writes approvals into one, the watcher
    reads another, and nothing is ever versioned.
    """
    p = Path(value)
    return str(p if p.is_absolute() else PROJECT_ROOT / p)


def get_schema_path():
    """schema.sql is packaged code (spec 030), not PROJECT_ROOT-anchored
    user data - resolved via importlib.resources so it's found correctly
    both in a source checkout and a real installed (packaged) build,
    regardless of cwd or where PROJECT_ROOT resolves to. SCHEMA_PATH, if
    set, still wins - the one explicit override this never had a reason
    to lose."""
    load_dotenv(PROJECT_ROOT / ".env")
    override = os.getenv("SCHEMA_PATH")
    if override:
        return anchored(override)
    return str(resources.files("vcs.db").joinpath("schema.sql"))

def get_db_url():
    load_dotenv(PROJECT_ROOT / ".env")
    MODE = os.getenv("MODE", _DEFAULT_MODE)
    if MODE == "dev":
        load_dotenv(PROJECT_ROOT / ".env.dev")
        default_db_url = "data/db-dev.sqlite"
    else:
        load_dotenv(PROJECT_ROOT / ".env.prod")
        default_db_url = "data/db.sqlite"
    # .env.dev/.env.prod are gitignored, dev-machine-only files (spec
    # 029) - a packaged install never has one, so DATABASE_URL must have
    # a real default instead of silently resolving to None and crashing
    # sqlite3.connect(None, ...) later.
    db_url = os.getenv("DATABASE_URL") or default_db_url
    return anchored(db_url)

def get_config_path():
    load_dotenv(PROJECT_ROOT / ".env")
    # Defaulted: returning None here detonates later inside path_normalize
    # when the Config*Event dataclasses build their default src.
    return anchored(os.getenv("CONFIG_PATH") or "config.yaml")

def get_http_api_key():
    load_dotenv(PROJECT_ROOT / ".env")
    return os.getenv("HTTP_API_KEY") or None

def save_to_file(data, file_path, mode="wb", encoding=DEFAULT_ENCODING):
    """Write `data` to `file_path`.

    Text modes are written as UTF-8 with newline translation disabled.
    Without an explicit encoding this used the platform default (cp1252 on
    Windows), which mangles or rejects non-ASCII content; without
    newline="" a write would rewrite every \\n as \\r\\n on Windows,
    mutating content and producing spurious versions.

    Encoding stays strict: UTF-8 represents any valid str, so a failure here
    means genuinely malformed input (a lone surrogate) that callers should
    see rather than silently mangle.
    """
    if "b" in mode:
        with open(file_path, mode) as f:
            f.write(data)
        return
    with open(file_path, mode, encoding=encoding, newline="") as f:
        f.write(data)

def read_text_file(file_path: str, encoding=DEFAULT_ENCODING) -> tuple[str, bool]:
    """Read `file_path` as text. Returns (content, lossy).

    Never raises on undecodable bytes: decodes strictly first, and only on
    failure falls back to errors="replace". `lossy` is True exactly when that
    fallback fired, so a caller can tell it is holding degraded content -
    typically a binary file - before acting on it or writing it back.

    Reads bytes and decodes explicitly rather than using text mode, because
    text mode can neither report that replacement happened nor leave line
    endings untranslated.
    """
    raw = read_file(file_path, mode="rb")
    try:
        return raw.decode(encoding), False
    except UnicodeDecodeError:
        return raw.decode(encoding, errors="replace"), True

def read_file(file_path: str, mode: str = 'rb', encoding=DEFAULT_ENCODING):
    file_path = path_normalize(file_path)
    if "b" in mode:
        with open(file_path, mode) as f:
            return f.read()
    with open(file_path, mode, encoding=encoding, newline="") as f:
        return f.read()

def collect_files(path: str) -> list[Path]:
    path = path_normalize(path)
    p = Path(path).resolve()

    if not p.exists():
        return []

    if p.is_file():
        return [path_normalize(p)]

    if p.is_dir():
        return [path_normalize(f) for f in p.rglob("*") if f.is_file()]

    return []

def path_normalize(path: str) -> str:
    p = Path(path).expanduser()
    if not p.is_absolute() and _WINDOWS_DRIVE_RE.match(str(path)):
        # A Windows-absolute path (drive letter + separator) whose host
        # pathlib flavour doesn't recognize it as absolute - PurePosixPath
        # has no concept of a drive letter, so on a non-Windows host
        # resolve(strict=False) below would silently treat it as relative
        # and prefix it with the CWD. PureWindowsPath parses drive+root
        # without touching the real filesystem, so this is deterministic
        # regardless of which OS runs it (e.g. a watch target string
        # exercised on Linux CI, or migrated from a Windows machine).
        return PureWindowsPath(path).as_posix()
    p = p.resolve(strict=False)
    return p.as_posix()
    

def make_dirs(*path: Path):
    for p in path:
        p.mkdir(parents=True, exist_ok=True)

def text_similarity(text_1: str, text_2: str):
    from difflib import SequenceMatcher
    matcher = SequenceMatcher(None, text_1, text_2)

    ratio = matcher.real_quick_ratio()
    if ratio < 0.5:
        return ratio
    
    ratio = matcher.quick_ratio()
    if ratio < 0.8:
        return ratio
    
    return matcher.ratio()

def bytes_to_string(input: bytes):
    return input.decode("utf-8", errors="replace")

def gen_hash(input: str):
    return hashlib.sha256(input).hexdigest()

def gen_ulid():
    return str(ULID())

def get_path_stats(path: Path):
    path = Path(path_normalize(path))
    stat = path.stat()
    return {
        "st_ino": str(stat.st_ino),
        "st_dev": str(stat.st_dev)
    }

