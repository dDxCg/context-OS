import os
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from filelock import FileLock

# One lock per repo path, not one global lock - unrelated repos must not
# block each other once multiple mirror repos exist (see docs/agents/STATE.md's
# per-directory design). Guarded by _locks_guard so two threads racing to
# create the *first* lock for a given path can't end up with two different
# Lock objects for the same repo.
#
# A real cross-process file lock, not threading.Lock: the CLI and HTTP API
# run as separate OS processes from the daemon, and a threading.Lock can't
# see across that boundary - two processes racing real `git` subprocess
# calls against the same mirror repo can interleave and corrupt it.
# filelock.FileLock is documented thread-safe when the same instance is
# reused, so this dict-of-locks pattern still serializes the daemon's own
# worker threads exactly as before.
_locks: dict[str, FileLock] = {}
_locks_guard = threading.Lock()

# filelock.FileLock defaults to timeout=-1 (block forever, issue #29) - a
# contended lock (now realistic with multiple concurrent MCP sessions
# sharing a project) would otherwise hang indefinitely instead of failing
# fast. Matches DBHandler.from_url()'s existing timeout=30.0 convention
# (issue #20/spec 016) - same tradeoff, already made once in this codebase.
LOCK_TIMEOUT: float = 30.0

# Per-git-call bound (spec 037). Sized against LOCK_TIMEOUT, not picked in
# isolation: write()/move() run six git calls under one lock, so the
# worst-case hold is 6 * GIT_TIMEOUT = 24s against a 30s wait. The 6s
# margin is the point - at 6 * 5s the two bounds are equal and a waiter
# could give up at the same instant a legitimately-working holder
# finishes. Local plumbing commands on a small bare repo normally take
# single-digit milliseconds.
GIT_TIMEOUT: float = 4.0


def _lock_for(repo_path: Path) -> FileLock:
    key = str(repo_path.resolve())
    with _locks_guard:
        if key not in _locks:
            _locks[key] = FileLock(str(repo_path / ".chrono-ctx.lock"), timeout=LOCK_TIMEOUT)
        return _locks[key]


def _run_git(args, repo_path: Path, *, check: bool = True, text: bool = False,
             input: bytes | None = None, env: dict | None = None):
    """Every git invocation goes through here (spec 037), so the timeout,
    stdin and prompt policy is applied in one place instead of being
    re-derived - or forgotten - at 24 call sites.

    stdin=DEVNULL matters most in the MCP server, where the parent's stdin
    is the JSON-RPC pipe: a git subprocess that inherits it can block
    forever reading a stream that will never carry an answer, and steal
    protocol bytes while doing it. GIT_TERMINAL_PROMPT=0 is the other half
    - DEVNULL alone turns a prompt into a confusing EOF read, this makes
    git refuse to prompt at all. Calls that pass `input` already get a
    pipe, so their stdin is left alone.
    """
    kwargs = {}
    if input is not None:
        kwargs["input"] = input
    else:
        kwargs["stdin"] = subprocess.DEVNULL
    return subprocess.run(
        args,
        cwd=str(repo_path),
        check=check,
        capture_output=True,
        text=text,
        timeout=GIT_TIMEOUT,
        env={**(env if env is not None else os.environ), "GIT_TERMINAL_PROMPT": "0"},
        **kwargs,
    )


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


def _is_initialized(repo_path: Path) -> bool:
    """Either shape counts: `.git/` (old-style, non-bare - every mirror
    repo created before spec 025) or HEAD+objects/ directly under
    repo_path (new-style, bare)."""
    if (repo_path / ".git").is_dir():
        return True
    return (repo_path / "HEAD").is_file() and (repo_path / "objects").is_dir()


def _require_initialized(repo_path: Path) -> None:
    if not _is_initialized(repo_path):
        raise RepoNotInitializedError(
            f"{repo_path} has no .git - call init_repo() first"
        )


def init_repo(repo_path: Path) -> None:
    """Create repo_path (and parents) if needed, `git init --bare` if not
    already a repo (of either shape - see _is_initialized). Idempotent.

    A genuinely new repo is created bare (spec 025): no working tree, so
    write()/remove()/move() never duplicate a file's current content
    on disk beyond the object store. Already-initialized repos (bare or
    not - every repo from before spec 025 is non-bare) are left alone
    entirely, not just left non-bare - running `git init --bare` again
    against an existing *non-bare* repo would create a second, orphaned
    bare git-dir alongside the real `.git/`, silently hiding its history,
    not convert it. write()/remove()/move() work correctly against a
    pre-existing non-bare repo unmodified (spec 025 AC-9) - no migration
    needed for old repos to keep working, only new repos get the storage
    win immediately.

    Serialized via the same cross-process lock write()/remove()/move() use
    (spec 022, issue #25) - two processes racing `git init`/`git
    config` against the same repo can interleave and fail (CalledProcessError
    exit 128, observed live)."""
    repo_path.mkdir(parents=True, exist_ok=True)
    with _lock_for(repo_path):
        if not _is_initialized(repo_path):
            try:
                _run_git(["git", "init", "--bare"], repo_path)
            except FileNotFoundError as exc:
                raise GitNotAvailableError(
                    "no `git` binary found on PATH"
                ) from exc
        # Local, per-repo committer identity - independent of any global git
        # config on the host, and separate from --author (the actor), which
        # write() sets per commit.
        _run_git(["git", "config", "user.email", "vcs@chrono-ctx.local"], repo_path)
        _run_git(["git", "config", "user.name", "chrono-ctx"], repo_path)


def _parse_author(author: str) -> tuple[str, str]:
    """"Name <email>" -> ("Name", "email") - commit-tree has no --author
    flag like porcelain `git commit`; author/committer identity is set via
    GIT_AUTHOR_*/GIT_COMMITTER_* env vars instead."""
    name, _, rest = author.partition("<")
    return name.strip(), rest.rstrip(">").strip()


def _commit_env(author: str) -> dict:
    name, email = _parse_author(author)
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": email,
        "GIT_COMMITTER_NAME": "chrono-ctx", "GIT_COMMITTER_EMAIL": "vcs@chrono-ctx.local",
    }


def _hash_object(repo_path: Path, content: bytes) -> str:
    result = _run_git(
        ["git", "hash-object", "-w", "--stdin"],
        repo_path, input=content,
    )
    return result.stdout.decode().strip()


def _write_tree(repo_path: Path) -> str:
    result = _run_git(
        ["git", "write-tree"], repo_path, text=True,
    )
    return result.stdout.strip()


def _tree_of(repo_path: Path, commit: str) -> str:
    result = _run_git(
        ["git", "rev-parse", f"{commit}^{{tree}}"],
        repo_path, text=True,
    )
    return result.stdout.strip()


def _commit_tree(repo_path: Path, tree_sha: str, parent: str | None, message: str, author: str) -> str:
    cmd = ["git", "commit-tree", tree_sha, "-m", message]
    if parent is not None:
        cmd += ["-p", parent]
    result = _run_git(
        cmd, repo_path, text=True,
        env=_commit_env(author),
    )
    return result.stdout.strip()


def _update_ref_head(repo_path: Path, new_rev: str) -> None:
    _run_git(
        ["git", "update-ref", "HEAD", new_rev],
        repo_path,
    )


def write(repo_path: Path, relpath: str, content: bytes, message: str, author: str) -> str:
    """Hash content into the object store, stage it in the index, and
    commit - no working-tree file is ever created (spec 025). The index
    persists across calls (a plain file under the repo dir, independent of
    any working tree) and already reflects HEAD's tree from prior calls -
    `--add` both adds a never-seen path and updates an already-tracked
    one, so this works identically for the first write to a path and every
    later overwrite."""
    _require_initialized(repo_path)
    with _lock_for(repo_path):
        blob_sha = _hash_object(repo_path, content)
        _run_git(
            ["git", "update-index", "--add", "--cacheinfo", f"100644,{blob_sha},{relpath}"],
            repo_path,
        )
        new_tree = _write_tree(repo_path)
        parent = _rev_parse_or_none(repo_path, "HEAD")
        if parent is not None and new_tree == _tree_of(repo_path, parent):
            # Nothing changed - content is byte-identical to what's already
            # committed. Not an error: return the rev it's already at.
            return parent
        new_rev = _commit_tree(repo_path, new_tree, parent, message, author)
        _update_ref_head(repo_path, new_rev)
        return new_rev


def head_rev(repo_path: Path, relpath: str) -> str | None:
    """SHA of the most recent commit touching relpath, or None if relpath
    has no commit history in this repo. A repo with zero commits at all
    (unborn HEAD - reachable when init_repo() ran but every write since has
    no-op'd, e.g. a delete of something never tracked) makes `git log` exit
    non-zero rather than print nothing; both cases mean "no history"."""
    _require_initialized(repo_path)
    result = _run_git(
        ["git", "log", "-1", "--format=%H", "--", relpath],
        repo_path, check=False, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _rev_parse_or_none(repo_path: Path, ref: str) -> str | None:
    """Like _rev_parse, but None instead of raising for an unborn HEAD (a
    repo with zero commits yet - ref simply doesn't resolve). --verify,
    not plain rev-parse: on a *bare* repo specifically, plain `git
    rev-parse HEAD` on an unborn HEAD exits 0 and prints the literal
    string "HEAD" unresolved (confirmed by experiment - a non-bare repo
    correctly exits non-zero for the same case). --verify guarantees a
    real SHA-1 or a non-zero exit, identically for both repo shapes."""
    result = _run_git(
        ["git", "rev-parse", "--verify", ref], repo_path, check=False, text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def remove(repo_path: Path, relpath: str, message: str, author: str) -> str | None:
    """Remove relpath (file or directory subtree) from the mirror and
    commit. Returns the new rev, or None if nothing was actually removed
    (relpath, and everything under it, already untracked at HEAD - matches
    the old `--ignore-unmatch` no-op contract). `git rm --cached` (index-
    only, `-r` recurses a directory prefix exactly like the old porcelain
    `git rm -r` did) instead of `git update-index --force-remove`, which
    (confirmed by experiment) refuses to run at all without a work tree -
    `--cached` is the one `git rm` mode that operates purely on the index."""
    _require_initialized(repo_path)
    with _lock_for(repo_path):
        parent = _rev_parse_or_none(repo_path, "HEAD")
        if parent is None or not path_exists_at_rev(repo_path, relpath, parent):
            return None
        _run_git(
            ["git", "rm", "--cached", "-r", "--ignore-unmatch", "--quiet", relpath],
            repo_path,
        )
        new_tree = _write_tree(repo_path)
        new_rev = _commit_tree(repo_path, new_tree, parent, message, author)
        _update_ref_head(repo_path, new_rev)
        return new_rev


def _ls_tree_entry(repo_path: Path, rev: str, relpath: str) -> tuple[str, str]:
    """(mode, blob_sha) for relpath in rev's tree."""
    result = _run_git(
        ["git", "ls-tree", rev, "--", relpath],
        repo_path, text=True,
    )
    meta, _, _ = result.stdout.strip().partition("\t")
    mode, _obj_type, sha = meta.split(" ")
    return mode, sha


def move(repo_path: Path, src_relpath: str, dst_relpath: str, message: str, author: str) -> str | None:
    """Relocate src_relpath's tracked blob to dst_relpath and commit.
    Returns the new rev, or None if src_relpath isn't tracked at HEAD
    (nothing to move) - reuses the blob already in the tree by SHA, never
    re-reads content from disk (there's no working-tree file to read)."""
    _require_initialized(repo_path)
    with _lock_for(repo_path):
        parent = _rev_parse_or_none(repo_path, "HEAD")
        if parent is None or not path_exists_at_rev(repo_path, src_relpath, parent):
            return None
        mode, blob_sha = _ls_tree_entry(repo_path, parent, src_relpath)
        _run_git(
            ["git", "update-index", "--add", "--cacheinfo", f"{mode},{blob_sha},{dst_relpath}"],
            repo_path,
        )
        _run_git(
            ["git", "rm", "--cached", "-r", "--ignore-unmatch", "--quiet", src_relpath],
            repo_path,
        )
        new_tree = _write_tree(repo_path)
        new_rev = _commit_tree(repo_path, new_tree, parent, message, author)
        _update_ref_head(repo_path, new_rev)
        return new_rev


def reset_stale_index(repo_path: Path) -> None:
    """Discard any index entry left staged-but-never-committed by a
    force-killed write()/remove()/move() (spec 025's plumbing runs several
    separate git calls under one lock - a kill between the index stage and
    the final commit leaves the index disagreeing with HEAD). Safe
    unconditionally, not a heuristic: the lock this acquires is held for
    the *entire* stage-through-commit sequence of every real write, so if
    it's free, no write is legitimately in progress - any index entry that
    doesn't match HEAD's tree can only be crash leftover (spec 027)."""
    _require_initialized(repo_path)
    with _lock_for(repo_path):
        head = _rev_parse_or_none(repo_path, "HEAD")
        if head is not None:
            _run_git(
                ["git", "read-tree", head],
                repo_path,
            )
        else:
            # Unborn HEAD - no tree to reset to, but a stray staged entry
            # from a crash before this repo's first-ever commit is still
            # possible and still needs discarding.
            _run_git(
                ["git", "read-tree", "--empty"],
                repo_path,
            )


def existing_mirror_repos(base_dir: Path) -> list[Path]:
    """Every git repo (bare or non-bare) under base_dir, at any depth -
    mirror repos nest at repo_dir_name()-derived depth, not as flat
    children of base_dir. Does not descend into a found repo's own
    internals (.git/, or objects/ for a bare repo) once identified."""
    if not base_dir.is_dir():
        return []
    found = []
    for root, dirs, _files in os.walk(base_dir):
        root_path = Path(root)
        if _is_initialized(root_path):
            found.append(root_path)
            dirs[:] = []
    return found


def reset_stale_indexes(base_dir: Path) -> list[Path]:
    """reset_stale_index() for every existing mirror repo under base_dir.
    Returns the repos it touched."""
    repos = existing_mirror_repos(base_dir)
    for repo_path in repos:
        reset_stale_index(repo_path)
    return repos


def commit_info(repo_path: Path, relpath: str) -> CommitInfo | None:
    """Author + timestamp of the commit head_rev() resolves to, or None if
    relpath has no commit history in this repo."""
    _require_initialized(repo_path)
    rev = head_rev(repo_path, relpath)
    if rev is None:
        return None
    result = _run_git(
        ["git", "log", "-1", "--format=%an <%ae>|%aI", "--", relpath],
        repo_path, text=True,
    )
    author, timestamp = result.stdout.strip().split("|", 1)
    return CommitInfo(rev=rev, author=author, timestamp=timestamp)


def log_history(repo_path: Path, relpath: str) -> list[dict]:
    """[{"rev", "author", "timestamp", "message"}, ...] for every commit
    touching relpath, newest first. Empty list if relpath has no history
    (including an unborn-HEAD repo, same non-zero-exit case as head_rev)."""
    _require_initialized(repo_path)
    result = _run_git(
        ["git", "log", "--follow", "--format=%H|%an <%ae>|%aI|%s", "--", relpath],
        repo_path, check=False, text=True,
    )
    if result.returncode != 0:
        return []
    commits = []
    for line in result.stdout.strip().splitlines():
        rev, author, timestamp, message = line.split("|", 3)
        commits.append({"rev": rev, "author": author, "timestamp": timestamp, "message": message})
    return commits


def diff(repo_path: Path, relpath: str, rev1: str, rev2: str) -> str:
    """Unified diff text of relpath between rev1 and rev2."""
    _require_initialized(repo_path)
    result = _run_git(
        ["git", "diff", rev1, rev2, "--", relpath],
        repo_path, text=True,
    )
    return result.stdout


def path_exists_at_rev(repo_path: Path, relpath: str, rev: str) -> bool:
    """Whether relpath existed in rev's tree. Not existing is a normal,
    expected outcome here (e.g. rev predates the path ever being created in
    a shared mirror repo - spec 024, issue #28), not an error - unlike
    show(), this never raises for a missing path."""
    _require_initialized(repo_path)
    result = _run_git(
        ["git", "cat-file", "-e", f"{rev}:{relpath}"],
        repo_path, check=False,
    )
    return result.returncode == 0


def show(repo_path: Path, relpath: str, rev: str) -> bytes:
    """Content of relpath as of rev."""
    _require_initialized(repo_path)
    result = _run_git(
        ["git", "show", f"{rev}:{relpath}"],
        repo_path,
    )
    return result.stdout


def commits_by_author(repo_path: Path, author_name: str) -> list[dict]:
    """Every commit in repo_path whose author *name* (the part before the
    email, i.e. write()'s actor label) exactly matches author_name, oldest
    first: [{"rev", "parent", "timestamp", "paths"}, ...]. `parent` is None
    for a root commit. One `git show` per matching commit to get its paths
    unambiguously - safer than parsing --name-only interleaved with a
    custom format line, since a path can contain "|"."""
    _require_initialized(repo_path)
    result = _run_git(
        ["git", "log", "--reverse", "--format=%H|%P|%an|%aI"],
        repo_path, check=False, text=True,
    )
    if result.returncode != 0:
        return []
    commits = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        rev, parents, author, timestamp = line.split("|", 3)
        if author != author_name:
            continue
        parent = parents.split()[0] if parents else None
        paths_result = _run_git(
            ["git", "show", "--format=", "--name-only", rev],
            repo_path, text=True,
        )
        paths = [p for p in paths_result.stdout.strip().splitlines() if p]
        commits.append({"rev": rev, "parent": parent, "timestamp": timestamp, "paths": paths})
    return commits


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
