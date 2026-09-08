import hashlib
import os
import re
from ulid import ULID
from dotenv import load_dotenv
from pathlib import Path, PureWindowsPath

DEFAULT_ENCODING = "utf-8"

_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:[\\/]")

# src/utils/helper.py -> src/utils -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[2]


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
    load_dotenv(PROJECT_ROOT / ".env")
    return anchored(os.getenv("SCHEMA_PATH", "data/schema.sql"))

def get_db_url():
    load_dotenv(PROJECT_ROOT / ".env")
    MODE = os.getenv("MODE", "dev")
    if MODE == "dev":
        load_dotenv(PROJECT_ROOT / ".env.dev")
    else:
        load_dotenv(PROJECT_ROOT / ".env.prod")
    db_url = os.getenv("DATABASE_URL")
    return anchored(db_url) if db_url else db_url

def get_config_path():
    load_dotenv(PROJECT_ROOT / ".env")
    # Defaulted: returning None here detonates later inside path_normalize
    # when the Config*Event dataclasses build their default src.
    return anchored(os.getenv("CONFIG_PATH") or "config.yaml")

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

