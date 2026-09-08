import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

# One lock per repo path, not one global lock - unrelated repos must not
# block each other once multiple mirror repos exist (git-backend-plan.md's
# per-directory design). Guarded by _locks_guard so two threads racing to
# create the *first* lock for a given path can't end up with two different
# Lock objects for the same repo.
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def _lock_for(repo_path: Path) -> threading.Lock:
    key = str(repo_path.resolve())
    with _locks_guard:
        if key not in _locks:
            _locks[key] = threading.Lock()
        return _locks[key]


class GitNotAvailableError(RuntimeError):
    """No `git` binary on PATH."""


class RepoNotInitializedError(RuntimeError):
    """Operation attempted on a repo_path never passed to init_repo()."""


@dataclass
class CommitInfo:
    rev: str
    author: str
    timestamp: str


class ConcurrentEditError(RuntimeError):
    """write_with_check()'s expected_rev no longer matches head_rev."""

    def __init__(self, path: str, expected_rev: str | None, current_rev: str,
                 current_author: str, current_timestamp: str):
        super().__init__(
            f"{path}: expected_rev {expected_rev!r} is stale, "
            f"current is {current_rev!r} (by {current_author} at {current_timestamp})"
        )
        self.path = path
        self.expected_rev = expected_rev
        self.current_rev = current_rev
        self.current_author = current_author
        self.current_timestamp = current_timestamp


def _require_initialized(repo_path: Path) -> None:
    if not (repo_path / ".git").is_dir():
        raise RepoNotInitializedError(
            f"{repo_path} has no .git - call init_repo() first"
        )


def init_repo(repo_path: Path) -> None:
    """Create repo_path (and parents) if needed, run `git init` if not
    already a repo. Idempotent."""
    repo_path.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "init"],
            cwd=str(repo_path),
            check=True,
            capture_output=True,
        )
    except FileNotFoundError as exc:
        raise GitNotAvailableError(
            "no `git` binary found on PATH"
        ) from exc
    # Local, per-repo committer identity - independent of any global git
    # config on the host, and separate from --author (the actor), which
    # write() sets per commit.
    subprocess.run(
        ["git", "config", "user.email", "vcs@chrono-ctx.local"],
        cwd=str(repo_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "chrono-ctx"],
        cwd=str(repo_path), check=True, capture_output=True,
    )


def write(repo_path: Path, relpath: str, content: bytes, message: str, author: str) -> str:
    """Write content to repo_path/relpath and commit it."""
    _require_initialized(repo_path)
    with _lock_for(repo_path):
        target = repo_path / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

        subprocess.run(
            ["git", "add", relpath],
            cwd=str(repo_path), check=True, capture_output=True,
        )

        staged_diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=str(repo_path),
        )
        if staged_diff.returncode == 0:
            # Nothing staged - content is byte-identical to what's already
            # committed. Not an error: return the rev it's already at.
            return _rev_parse(repo_path, "HEAD")

        subprocess.run(
            ["git", "commit", f"--author={author}", "-m", message],
            cwd=str(repo_path), check=True, capture_output=True,
        )
        return _rev_parse(repo_path, "HEAD")


def head_rev(repo_path: Path, relpath: str) -> str | None:
    """SHA of the most recent commit touching relpath, or None if relpath
    has no commit history in this repo."""
    _require_initialized(repo_path)
    result = subprocess.run(
        ["git", "log", "-1", "--format=%H", "--", relpath],
        cwd=str(repo_path), check=True, capture_output=True, text=True,
    )
    return result.stdout.strip() or None


def _rev_parse(repo_path: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", ref],
        cwd=str(repo_path), check=True, capture_output=True, text=True,
    )
    return result.stdout.strip()


def commit_info(repo_path: Path, relpath: str) -> CommitInfo | None:
    """Author + timestamp of the commit head_rev() resolves to, or None if
    relpath has no commit history in this repo."""
    _require_initialized(repo_path)
    rev = head_rev(repo_path, relpath)
    if rev is None:
        return None
    result = subprocess.run(
        ["git", "log", "-1", "--format=%an <%ae>|%aI", "--", relpath],
        cwd=str(repo_path), check=True, capture_output=True, text=True,
    )
    author, timestamp = result.stdout.strip().split("|", 1)
    return CommitInfo(rev=rev, author=author, timestamp=timestamp)


def write_with_check(
    repo_path: Path, relpath: str, content: bytes, message: str, author: str,
    expected_rev: str | None = None, force: bool = False,
) -> str:
    """Like write(), but refuses if relpath's current head_rev has moved
    past expected_rev since the caller last read it. expected_rev=None
    skips the check entirely (today's write() behavior)."""
    if expected_rev is not None and not force:
        current = head_rev(repo_path, relpath)
        if current != expected_rev:
            info = commit_info(repo_path, relpath)
            raise ConcurrentEditError(
                path=relpath,
                expected_rev=expected_rev,
                current_rev=current,
                current_author=info.author if info else "",
                current_timestamp=info.timestamp if info else "",
            )
    return write(repo_path, relpath, content, message, author)
