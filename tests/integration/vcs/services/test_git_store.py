import subprocess
import threading

import filelock
import pytest

from vcs.services import git_store


def test_ac1_init_repo_creates_a_bare_repo(repo_path):
    """Spec 025 AC-1: no working tree - HEAD/objects directly under
    repo_path, no .git subdirectory."""
    git_store.init_repo(repo_path)

    assert not (repo_path / ".git").exists()
    assert (repo_path / "HEAD").is_file()
    assert (repo_path / "objects").is_dir()
    result = subprocess.run(
        ["git", "-C", str(repo_path), "rev-parse", "--is-bare-repository"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "true"


def test_init_repo_does_not_reinitialize_an_already_initialized_repo(initialized_repo):
    """Guards the corruption risk spec 025 flags: running `git init --bare`
    again against a repo that already has content would be harmless by
    itself, but running it against a pre-existing *non-bare* repo (old
    mirrors, see AC-9) would create a second, orphaned bare git-dir
    alongside the real one. init_repo() must not re-run `git init` at all
    once a repo already exists, of either shape."""
    rev = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    git_store.init_repo(initialized_repo)

    assert git_store.head_rev(initialized_repo, "a.txt") == rev


def test_ac1_init_repo_blocks_while_repo_lock_held(repo_path):
    """Spec 022 AC-1 / issue #25: init_repo() must serialize against the same
    cross-process lock write()/remove()/move() already use, or two processes
    racing init_repo() on the same repo can interleave `git init`/`git
    config` subprocess calls and corrupt/error (CalledProcessError exit
    128, observed live)."""
    lock = git_store._lock_for(repo_path)
    lock.acquire()
    done = threading.Event()

    def call_init():
        git_store.init_repo(repo_path)
        done.set()

    thread = threading.Thread(target=call_init)
    thread.start()
    try:
        assert not done.wait(timeout=0.3), "init_repo() ran while the repo lock was held"
    finally:
        lock.release()

    assert done.wait(timeout=2.0), "init_repo() never completed after the lock was released"
    thread.join()


def test_ac2_write_creates_first_commit_with_message_and_author(initialized_repo):
    rev = git_store.write(
        initialized_repo,
        "a.txt",
        b"hello",
        message="add a.txt",
        author="Test Author <test@chrono-ctx.local>",
    )

    assert not (initialized_repo / "a.txt").exists(), "no working-tree file should be created (spec 025)"
    assert git_store.show(initialized_repo, "a.txt", rev) == b"hello"

    log = subprocess.run(
        ["git", "-C", str(initialized_repo), "log", "--format=%H|%s|%an <%ae>"],
        capture_output=True,
        text=True,
        check=True,
    )
    lines = log.stdout.strip().splitlines()
    assert len(lines) == 1
    commit_hash, message, author = lines[0].split("|", 2)
    assert message == "add a.txt"
    assert author == "Test Author <test@chrono-ctx.local>"
    assert rev == commit_hash


def test_ac3_write_identical_content_is_noop(initialized_repo):
    first_rev = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    second_rev = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="rewrite a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert second_rev == first_rev
    log = subprocess.run(
        ["git", "-C", str(initialized_repo), "log", "--format=%H"],
        capture_output=True, text=True, check=True,
    )
    assert len(log.stdout.strip().splitlines()) == 1


def test_ac4_write_different_content_creates_new_commit(initialized_repo):
    first_rev = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    second_rev = git_store.write(
        initialized_repo, "a.txt", b"world",
        message="update a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert second_rev != first_rev
    log = subprocess.run(
        ["git", "-C", str(initialized_repo), "log", "--format=%H"],
        capture_output=True, text=True, check=True,
    )
    assert len(log.stdout.strip().splitlines()) == 2


def test_ac4_write_two_paths_both_present_in_final_tree(initialized_repo):
    """Spec 025 AC-4: proves the index accumulates across calls (each
    write() only touches its own path via update-index) rather than each
    write starting the tree from a blank slate."""
    git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    git_store.write(
        initialized_repo, "b.txt", b"world",
        message="add b.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert git_store.show(initialized_repo, "a.txt", "HEAD") == b"hello"
    assert git_store.show(initialized_repo, "b.txt", "HEAD") == b"world"


def test_ac5_head_rev_returns_last_commit_touching_path_not_overall_head(initialized_repo):
    a_rev = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    b_rev = git_store.write(
        initialized_repo, "b.txt", b"world",
        message="add b.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert a_rev != b_rev
    assert git_store.head_rev(initialized_repo, "a.txt") == a_rev
    assert git_store.head_rev(initialized_repo, "b.txt") == b_rev


def test_ec2_head_rev_returns_none_for_untracked_path(initialized_repo):
    git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert git_store.head_rev(initialized_repo, "never-written.txt") is None


def test_ec1_write_on_uninitialized_repo_raises_repo_not_initialized(repo_path):
    with pytest.raises(git_store.RepoNotInitializedError):
        git_store.write(
            repo_path, "a.txt", b"hello",
            message="add a.txt", author="Test Author <test@chrono-ctx.local>",
        )

    with pytest.raises(git_store.RepoNotInitializedError):
        git_store.head_rev(repo_path, "a.txt")


def test_ec3_init_repo_raises_when_git_missing(repo_path, monkeypatch):
    def fake_run(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(git_store.subprocess, "run", fake_run)

    with pytest.raises(git_store.GitNotAvailableError):
        git_store.init_repo(repo_path)


def test_ac6_concurrent_write_no_index_lock_error(initialized_repo):
    errors = []

    def do_write(name, content):
        try:
            git_store.write(
                initialized_repo, name, content,
                message=f"add {name}", author="Test Author <test@chrono-ctx.local>",
            )
        except Exception as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=do_write, args=("a.txt", b"hello")),
        threading.Thread(target=do_write, args=("b.txt", b"world")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
        assert not t.is_alive()

    assert not errors, errors
    log = subprocess.run(
        ["git", "-C", str(initialized_repo), "log", "--format=%H"],
        capture_output=True, text=True, check=True,
    )
    assert len(log.stdout.strip().splitlines()) == 2


def test_ac1_expected_rev_none_writes_unconditionally(initialized_repo):
    first_rev = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    second_rev = git_store.write_with_check(
        initialized_repo, "a.txt", b"world",
        message="update a.txt", author="Test Author <test@chrono-ctx.local>",
        expected_rev=None,
    )

    assert second_rev != first_rev
    assert git_store.head_rev(initialized_repo, "a.txt") == second_rev


def test_ac2_matching_expected_rev_commits(initialized_repo):
    r1 = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    r2 = git_store.write_with_check(
        initialized_repo, "a.txt", b"world",
        message="update a.txt", author="Test Author <test@chrono-ctx.local>",
        expected_rev=r1,
    )

    assert r2 != r1
    assert git_store.head_rev(initialized_repo, "a.txt") == r2


def test_ac3_stale_expected_rev_raises_and_no_commit(initialized_repo):
    r1 = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    # Someone else commits in between - r1 is now stale.
    git_store.write(
        initialized_repo, "a.txt", b"from someone else",
        message="someone else's edit", author="Someone Else <else@chrono-ctx.local>",
    )
    r_current = git_store.head_rev(initialized_repo, "a.txt")

    with pytest.raises(git_store.ConcurrentEditError):
        git_store.write_with_check(
            initialized_repo, "a.txt", b"my stale edit",
            message="my edit", author="Test Author <test@chrono-ctx.local>",
            expected_rev=r1,
        )

    assert git_store.head_rev(initialized_repo, "a.txt") == r_current


def test_ac4_force_overrides_stale_expected_rev(initialized_repo):
    r1 = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    git_store.write(
        initialized_repo, "a.txt", b"from someone else",
        message="someone else's edit", author="Someone Else <else@chrono-ctx.local>",
    )

    r_forced = git_store.write_with_check(
        initialized_repo, "a.txt", b"my forced edit",
        message="force overwrite", author="Test Author <test@chrono-ctx.local>",
        expected_rev=r1, force=True,
    )

    assert git_store.head_rev(initialized_repo, "a.txt") == r_forced
    assert git_store.show(initialized_repo, "a.txt", "HEAD") == b"my forced edit"


def test_ac5_concurrent_edit_error_exposes_conflict_details(initialized_repo):
    r1 = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    r_current = git_store.write(
        initialized_repo, "a.txt", b"from someone else",
        message="someone else's edit", author="Someone Else <else@chrono-ctx.local>",
    )

    with pytest.raises(git_store.ConcurrentEditError) as excinfo:
        git_store.write_with_check(
            initialized_repo, "a.txt", b"my stale edit",
            message="my edit", author="Test Author <test@chrono-ctx.local>",
            expected_rev=r1,
        )

    err = excinfo.value
    assert err.path == "a.txt"
    assert err.expected_rev == r1
    assert err.current_rev == r_current
    assert err.current_author == "Someone Else <else@chrono-ctx.local>"
    assert err.current_timestamp  # non-empty, ISO 8601 from git %aI


def test_ec1_commit_info_returns_none_for_untracked_path(initialized_repo):
    git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert git_store.commit_info(initialized_repo, "never-written.txt") is None


def test_ec2_write_with_check_on_uninitialized_repo_raises(repo_path):
    with pytest.raises(git_store.RepoNotInitializedError):
        git_store.write_with_check(
            repo_path, "a.txt", b"hello",
            message="add a.txt", author="Test Author <test@chrono-ctx.local>",
            expected_rev="deadbeef",
        )


def test_remove_removes_file_and_commits(initialized_repo):
    git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    rev = git_store.remove(
        initialized_repo, "a.txt",
        message="remove a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert rev is not None
    assert not git_store.path_exists_at_rev(initialized_repo, "a.txt", "HEAD")
    assert git_store.head_rev(initialized_repo, "a.txt") == rev


def test_remove_is_noop_when_nothing_tracked(initialized_repo):
    rev = git_store.remove(
        initialized_repo, "never-written.txt",
        message="remove nothing", author="Test Author <test@chrono-ctx.local>",
    )

    assert rev is None


def test_remove_removes_directory_subtree(initialized_repo):
    git_store.write(
        initialized_repo, "dir/a.txt", b"a",
        message="add dir/a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    git_store.write(
        initialized_repo, "dir/b.txt", b"b",
        message="add dir/b.txt", author="Test Author <test@chrono-ctx.local>",
    )

    rev = git_store.remove(
        initialized_repo, "dir",
        message="remove dir", author="Test Author <test@chrono-ctx.local>",
    )

    assert rev is not None
    assert not git_store.path_exists_at_rev(initialized_repo, "dir/a.txt", "HEAD")
    assert not git_store.path_exists_at_rev(initialized_repo, "dir/b.txt", "HEAD")


def test_move_renames_file_and_commits(initialized_repo):
    git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    rev = git_store.move(
        initialized_repo, "a.txt", "b.txt",
        message="rename a.txt to b.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert rev is not None
    assert not git_store.path_exists_at_rev(initialized_repo, "a.txt", "HEAD")
    assert git_store.show(initialized_repo, "b.txt", "HEAD") == b"hello"

    log = subprocess.run(
        ["git", "-C", str(initialized_repo), "log", "--follow", "--format=%H", "--", "b.txt"],
        capture_output=True, text=True, check=True,
    )
    assert len(log.stdout.strip().splitlines()) == 2


def test_ac7_move_reuses_existing_blob_sha_without_rereading_content(initialized_repo):
    """Spec 025 AC-7: move must not need the content again at all (there's
    nowhere to read it from - no working tree) - it relocates the blob
    already in the tree by SHA."""
    rev1 = git_store.write(
        initialized_repo, "a.txt", b"hello",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    rev2 = git_store.move(
        initialized_repo, "a.txt", "b.txt",
        message="rename", author="Test Author <test@chrono-ctx.local>",
    )

    assert rev2 != rev1
    blob_before = subprocess.run(
        ["git", "-C", str(initialized_repo), "rev-parse", f"{rev1}:a.txt"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    blob_after = subprocess.run(
        ["git", "-C", str(initialized_repo), "rev-parse", f"{rev2}:b.txt"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert blob_before == blob_after


def test_move_is_noop_when_source_missing(initialized_repo):
    rev = git_store.move(
        initialized_repo, "never-written.txt", "elsewhere.txt",
        message="move nothing", author="Test Author <test@chrono-ctx.local>",
    )

    assert rev is None


def test_ac2_log_history_returns_commits_newest_first(initialized_repo):
    first_rev = git_store.write(
        initialized_repo, "a.txt", b"v1",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    second_rev = git_store.write(
        initialized_repo, "a.txt", b"v2",
        message="update a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    history = git_store.log_history(initialized_repo, "a.txt")

    assert [entry["rev"] for entry in history] == [second_rev, first_rev]
    assert history[0]["message"] == "update a.txt"
    assert history[0]["author"] == "Test Author <test@chrono-ctx.local>"
    assert history[1]["message"] == "add a.txt"


def test_log_history_empty_for_untracked_path(initialized_repo):
    assert git_store.log_history(initialized_repo, "never-written.txt") == []


def test_ac1_show_returns_content_of_relpath_at_rev(initialized_repo):
    first_rev = git_store.write(
        initialized_repo, "a.txt", b"v1",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    git_store.write(
        initialized_repo, "a.txt", b"v2",
        message="update a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert git_store.show(initialized_repo, "a.txt", first_rev) == b"v1"


def test_ac2_path_exists_at_rev_true_when_present_false_when_not(initialized_repo):
    """Spec 024 AC-2."""
    rev = git_store.write(
        initialized_repo, "a.txt", b"v1",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    assert git_store.path_exists_at_rev(initialized_repo, "a.txt", rev) is True
    assert git_store.path_exists_at_rev(initialized_repo, "never-written.txt", rev) is False


def test_ac2_lock_for_serializes_across_separate_lock_instances(initialized_repo):
    """Simulates cross-process locking: a second process would build its own
    FileLock object pointed at the same lock file, not share this one."""
    lock1 = git_store._lock_for(initialized_repo)
    lock2 = filelock.FileLock(lock1.lock_file)

    lock1.acquire()
    try:
        with pytest.raises(filelock.Timeout):
            lock2.acquire(timeout=0.05)
    finally:
        lock1.release()

    lock2.acquire(timeout=0.5)
    lock2.release()


def test_ac1_commits_by_author_returns_matching_commits_oldest_first(initialized_repo):
    r1 = git_store.write(
        initialized_repo, "a.txt", b"v1",
        message="add a.txt", author="agent:s1 <agent@chrono-ctx.local>",
    )
    r2 = git_store.write(
        initialized_repo, "b.txt", b"v1",
        message="add b.txt", author="agent:s1 <agent@chrono-ctx.local>",
    )

    commits = git_store.commits_by_author(initialized_repo, "agent:s1")

    assert [c["rev"] for c in commits] == [r1, r2]
    assert commits[0]["parent"] is None
    assert commits[0]["paths"] == ["a.txt"]
    assert commits[1]["parent"] == r1
    assert commits[1]["paths"] == ["b.txt"]
    assert commits[0]["timestamp"]


def test_ac2_commits_by_author_excludes_other_authors(initialized_repo):
    git_store.write(
        initialized_repo, "a.txt", b"v1",
        message="add a.txt", author="agent:s1 <agent@chrono-ctx.local>",
    )
    git_store.write(
        initialized_repo, "b.txt", b"v1",
        message="add b.txt", author="Someone Else <else@chrono-ctx.local>",
    )

    commits = git_store.commits_by_author(initialized_repo, "agent:s1")

    assert len(commits) == 1
    assert commits[0]["paths"] == ["a.txt"]


def test_commits_by_author_empty_for_unborn_head(repo_path):
    git_store.init_repo(repo_path)

    assert git_store.commits_by_author(repo_path, "agent:s1") == []


def test_ac9_write_against_a_preexisting_non_bare_repo_still_works(repo_path):
    """Spec 025 AC-9: every mirror repo created before this spec is
    non-bare (porcelain `git init`, real working tree, index already
    reflecting HEAD from prior `git add`s). write() must keep working
    against one unmodified - no forced migration required."""
    repo_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "vcs@chrono-ctx.local"],
        cwd=str(repo_path), check=True, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "chrono-ctx"],
        cwd=str(repo_path), check=True, capture_output=True,
    )
    (repo_path / "old.txt").write_bytes(b"old content")
    subprocess.run(["git", "add", "old.txt"], cwd=str(repo_path), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "--author=Old Author <old@chrono-ctx.local>", "-m", "old-style commit"],
        cwd=str(repo_path), check=True, capture_output=True,
    )
    assert not (repo_path / "HEAD").exists()  # sanity: genuinely non-bare, not accidentally bare

    new_rev = git_store.write(
        repo_path, "new.txt", b"new content",
        message="new-style commit", author="Test Author <test@chrono-ctx.local>",
    )

    assert git_store.show(repo_path, "old.txt", "HEAD") == b"old content"
    assert git_store.show(repo_path, "new.txt", new_rev) == b"new content"
    log = subprocess.run(
        ["git", "-C", str(repo_path), "log", "--format=%H"],
        capture_output=True, text=True, check=True,
    )
    assert len(log.stdout.strip().splitlines()) == 2


def test_ac3_diff_shows_unified_diff_between_two_revs(initialized_repo):
    first_rev = git_store.write(
        initialized_repo, "a.txt", b"line one\n",
        message="add a.txt", author="Test Author <test@chrono-ctx.local>",
    )
    second_rev = git_store.write(
        initialized_repo, "a.txt", b"line two\n",
        message="update a.txt", author="Test Author <test@chrono-ctx.local>",
    )

    text = git_store.diff(initialized_repo, "a.txt", first_rev, second_rev)

    assert "-line one" in text
    assert "+line two" in text
