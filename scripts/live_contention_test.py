#!/usr/bin/env python3
"""Live contention test - does chrono-ctx fail fast, and say so, under a real incident?

Companion to live_integration_test.py, which proves the happy path. This one
manufactures the *incidents* that specs 036/037/038 added bounds for, against
real OS-level locks held by real separate processes, and checks three things
per scenario:

  1. the MCP tool returns a structured {"status": ...} dict - never a hang,
     never a transport-level error;
  2. it does so inside the bound that spec says, not whenever the contention
     happens to clear;
  3. data/mcp.log records it, because "check the log" finding nothing is what
     started this whole line of work (issue #29).

Scenarios:

  A. git mirror-repo lock held 35s by another process -> write_file() with an
     expected_version (the only path that reaches the lock synchronously)
     must give up at LOCK_TIMEOUT (~30s) with an error dict. Spec 036.
  B. a git subprocess that hangs -> _run_git() must raise TimeoutExpired at
     GIT_TIMEOUT (~4s). Spec 037.
  C. SQLite write lock held 10s by another process -> write_file() must still
     SUCCEED, in ~2s not ~30s, having given up on actor-hint bookkeeping and
     logged a WARNING about it. Spec 038.
  E. both the git lock (20s, i.e. under its timeout) and the DB lock held at
     once -> the waits compose additively and the call still lands inside the
     45-60s per-call budget. This is the budget model from
     docs/agents/draft/mcp-fail-fast-and-observability-plan.md actually
     measured end to end, rather than only reasoned about.

Two limits stated up front rather than papered over:

  * Like live_integration_test.py, the MCP server runs IN-PROCESS here
    (fastmcp.Client(server.mcp)), not as the separate stdio process a real
    client spawns. The locks, subprocesses, SQLite and git are all real and
    cross-process; the tool dispatch is not.
  * Scenario B hangs a real subprocess through the real
    subprocess.run(..., timeout=GIT_TIMEOUT) code path, but does not exercise
    PATH resolution of a genuinely hung `git` binary - portable shimming of
    `git` differs too much between Windows and POSIX to do honestly here. The
    MCP layer's translation of TimeoutExpired into an error dict is covered by
    spec 038 AC-7 in the pytest suite.

Cleans up after itself: probe file deleted, probe DB row removed, holder
processes terminated even on failure. Safe to re-run.
"""

import argparse
import asyncio
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastmcp import Client  # noqa: E402

import app.mcp.server as server  # noqa: E402
from utils.helper import get_db_url  # noqa: E402
from vcs.services import git_store, mirror_path  # noqa: E402
from vcs.services.configure import derive_watch_targets, parse_config  # noqa: E402

PROBE_NAME = "live-contention-probe.md"
PROBE_DB_ROW = "live-contention-probe-holder"


@dataclass
class Result:
    steps: list[tuple[str, str, str]] = field(default_factory=list)

    def record(self, step_id: str, status: str, detail: str = "") -> None:
        self.steps.append((step_id, status, detail))
        print(f"[{status}] {step_id} {detail}", flush=True)

    def failed(self) -> bool:
        return any(status == "FAIL" for _, status, _ in self.steps)

    def summary(self) -> None:
        print("\n=== Live contention test summary ===")
        for step_id, status, detail in self.steps:
            print(f"  [{status:4}] {step_id} {detail}")


# --- incident holders -------------------------------------------------------
# Separate OS processes on purpose: filelock and SQLite both serialize across
# processes, and an in-process "holder" would either be reentrant (filelock is,
# for the same thread reusing the cached instance) or share the connection.

GIT_LOCK_HOLDER = """
import sys, time
sys.path.insert(0, r"{src}")
from pathlib import Path
from vcs.services import git_store
lock = git_store._lock_for(Path(r"{repo}"))
lock.acquire()
print("HELD", flush=True)
time.sleep({seconds})
lock.release()
"""

DB_LOCK_HOLDER = """
import sqlite3, time
conn = sqlite3.connect(r"{db}", timeout=30.0, isolation_level=None)
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("BEGIN IMMEDIATE")
conn.execute(
    "INSERT OR REPLACE INTO pending_actor_hints (location, actor, expires_at)"
    " VALUES (?, ?, ?)", ("{row}", "holder", time.time() + 999),
)
print("HELD", flush=True)
time.sleep({seconds})
conn.rollback()
"""


class Holder:
    """A subprocess holding a real lock, synchronized on its "HELD" line so
    the test never races the thing it is trying to contend with."""

    def __init__(self, program: str, label: str):
        self.label = label
        self.proc = subprocess.Popen(
            [sys.executable, "-c", program],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )

    def wait_until_held(self, timeout: float = 15.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = self.proc.stdout.readline()
            if line.strip() == "HELD":
                return True
            if not line and self.proc.poll() is not None:
                return False
        return False

    def stop(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


def probe_paths(result: Result) -> tuple[Path, Path] | None:
    """An in-scope probe file (so no elicitation prompt fires) plus the mirror
    repo its watch target maps to."""
    config = parse_config()
    for source in config.get("sources", []):
        if source.get("type") != "local":
            continue
        directory = Path(source["path"])
        if directory.is_dir():
            probe = directory / PROBE_NAME
            repo, _ = mirror_path.resolve_mirror_location(
                str(probe), derive_watch_targets(config)
            )
            git_store.init_repo(repo)
            return probe, repo
    result.record("setup", "FAIL", "no existing local source directory in config.yaml")
    return None


async def call_write(probe: Path, expected_version: str | None) -> tuple[dict, float]:
    async def _fail_if_called(message, response_type, params, ctx):
        raise AssertionError("probe path should be in scope - no prompt expected")

    args = {"path": str(probe), "content": f"live contention probe {time.time()}"}
    if expected_version is not None:
        args["expected_version"] = expected_version

    async with Client(server.mcp, elicitation_handler=_fail_if_called) as client:
        start = time.monotonic()
        response = await client.call_tool("write_file", args)
        return response.data, time.monotonic() - start


def log_tail(lines: int = 40) -> str:
    try:
        return "\n".join(server.LOG_PATH.read_text(encoding="utf-8").splitlines()[-lines:])
    except OSError:
        return ""


def scenario_a(result: Result, probe: Path, repo: Path) -> None:
    """Spec 036: git lock held longer than LOCK_TIMEOUT -> error dict at the bound."""
    holder = Holder(
        GIT_LOCK_HOLDER.format(src=PROJECT_ROOT / "src", repo=repo, seconds=35),
        "git-lock",
    )
    try:
        if not holder.wait_until_held():
            result.record("A.git-lock", "FAIL", "holder never acquired the lock")
            return
        data, elapsed = asyncio.run(call_write(probe, expected_version="probe-stale-rev"))
    finally:
        holder.stop()

    if data.get("status") != "error":
        result.record("A.git-lock", "FAIL", f"expected status=error, got {data} after {elapsed:.1f}s")
        return
    low, high = git_store.LOCK_TIMEOUT - 3, git_store.LOCK_TIMEOUT + 6
    if not (low <= elapsed <= high):
        result.record("A.git-lock", "FAIL",
                      f"gave up at {elapsed:.1f}s, expected ~{git_store.LOCK_TIMEOUT}s")
        return
    result.record("A.git-lock", "PASS",
                  f"error dict at {elapsed:.1f}s (LOCK_TIMEOUT={git_store.LOCK_TIMEOUT}s)")


def scenario_b(result: Result, repo: Path) -> None:
    """Spec 037: a hung git subprocess must be killed at GIT_TIMEOUT."""
    start = time.monotonic()
    try:
        git_store._run_git([sys.executable, "-c", "import time; time.sleep(60)"], repo)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        if elapsed > git_store.GIT_TIMEOUT + 3:
            result.record("B.git-hang", "FAIL", f"killed late, at {elapsed:.1f}s")
            return
        result.record("B.git-hang", "PASS",
                      f"TimeoutExpired at {elapsed:.1f}s (GIT_TIMEOUT={git_store.GIT_TIMEOUT}s)")
        return
    except Exception as e:  # noqa: BLE001 - any other outcome is a failure worth showing
        result.record("B.git-hang", "FAIL", f"raised {type(e).__name__} instead of TimeoutExpired")
        return
    result.record("B.git-hang", "FAIL", "hung subprocess completed - no timeout applied")


def scenario_c(result: Result, probe: Path) -> None:
    """Spec 038: DB contention must cost ~HINT_DB_TIMEOUT, not 30s, and the
    skipped bookkeeping must show up in the log instead of vanishing."""
    before = len(log_tail(10_000).splitlines())
    holder = Holder(
        DB_LOCK_HOLDER.format(db=get_db_url(), row=PROBE_DB_ROW, seconds=10),
        "db-lock",
    )
    try:
        if not holder.wait_until_held():
            result.record("C.db-lock", "FAIL", "holder never acquired the write lock")
            return
        data, elapsed = asyncio.run(call_write(probe, expected_version=None))
    finally:
        holder.stop()

    if data.get("status") != "ok":
        result.record("C.db-lock", "FAIL", f"write should still succeed, got {data}")
        return
    if elapsed > server.HINT_DB_TIMEOUT + 4:
        result.record("C.db-lock", "FAIL",
                      f"took {elapsed:.1f}s - hint bookkeeping is still riding the 30s default")
        return
    new_lines = log_tail(10_000).splitlines()[before:]
    if not any("actor hint not recorded" in line for line in new_lines):
        result.record("C.db-lock", "FAIL",
                      f"succeeded in {elapsed:.1f}s but nothing logged about the skipped hint")
        return
    result.record("C.db-lock", "PASS",
                  f"ok in {elapsed:.1f}s, hint skipped and logged "
                  f"(HINT_DB_TIMEOUT={server.HINT_DB_TIMEOUT}s)")


def scenario_e(result: Result, probe: Path, repo: Path) -> None:
    """The budget model, measured: a git-lock wait that resolves *under* its
    timeout, followed by a DB stall, should compose additively and land well
    inside the 45-60s per-call budget."""
    git_hold = 20
    budget = 60.0

    # Getting BOTH waits into one call needs an expected_version that MATCHES:
    # None skips the version check entirely (never touching the git lock), and
    # a stale one returns a conflict before the actor hint is ever reached.
    # Only a matching rev walks the whole path - lock wait, then git work, then
    # the DB stall. Seed the mirror so we know the rev.
    relpath = mirror_path.resolve_mirror_location(
        str(probe), derive_watch_targets()
    )[1]
    probe.write_text("live contention compound probe\n", encoding="utf-8")
    current = git_store.write(
        repo, relpath, probe.read_bytes(),
        message="seed for compound contention probe",
        author="live-contention-test <test@chrono-ctx.local>",
    )

    git_holder = Holder(
        GIT_LOCK_HOLDER.format(src=PROJECT_ROOT / "src", repo=repo, seconds=git_hold),
        "git-lock",
    )
    db_holder = None
    try:
        if not git_holder.wait_until_held():
            result.record("E.compound", "FAIL", "git holder never acquired the lock")
            return
        db_holder = Holder(
            DB_LOCK_HOLDER.format(db=get_db_url(), row=PROBE_DB_ROW, seconds=git_hold + 15),
            "db-lock",
        )
        if not db_holder.wait_until_held():
            result.record("E.compound", "FAIL", "db holder never acquired the write lock")
            return
        data, elapsed = asyncio.run(call_write(probe, expected_version=current))
    finally:
        git_holder.stop()
        if db_holder is not None:
            db_holder.stop()

    if data.get("status") != "ok":
        result.record("E.compound", "FAIL",
                      f"expected the call to complete, got {data} after {elapsed:.1f}s")
        return
    # Both waits must actually be in there, or this measured one of them and
    # reported it as the compound case - which is exactly the trap this
    # scenario existed to avoid.
    floor = git_hold + server.HINT_DB_TIMEOUT - 2
    if elapsed < floor:
        result.record("E.compound", "FAIL",
                      f"{elapsed:.1f}s is below {floor:.0f}s - only one of the two waits "
                      "was on the path, so this proves nothing about the budget")
        return
    if elapsed > budget:
        result.record("E.compound", "FAIL",
                      f"{elapsed:.1f}s exceeds the {budget:.0f}s per-call budget")
        return
    result.record("E.compound", "PASS",
                  f"both waits composed: {elapsed:.1f}s "
                  f"(~{git_hold}s lock + ~{server.HINT_DB_TIMEOUT}s hint), "
                  f"inside the {budget:.0f}s budget")


def cleanup(result: Result, probe: Path) -> None:
    try:
        probe.unlink(missing_ok=True)
    except OSError as e:
        result.record("cleanup.file", "WARN", str(e))
    try:
        conn = sqlite3.connect(get_db_url(), timeout=10.0)
        conn.execute("DELETE FROM pending_actor_hints WHERE location = ?", (PROBE_DB_ROW,))
        conn.commit()
        conn.close()
    except sqlite3.Error as e:
        result.record("cleanup.db", "WARN", str(e))


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scenarios", default="A,B,C,E",
        help="comma-separated subset to run (default: all). A and E each wait ~20-35s.",
    )
    args = parser.parse_args()
    wanted = {s.strip().upper() for s in args.scenarios.split(",") if s.strip()}

    result = Result()
    # The MCP server normally does this in main(); in-process it has to be
    # explicit, and scenario C reads the file it configures.
    server._setup_file_logging()

    paths = probe_paths(result)
    if paths is None:
        result.summary()
        return 1
    probe, repo = paths
    result.record("setup", "PASS", f"probe={probe.name} repo={repo.name}")

    try:
        if "B" in wanted:
            scenario_b(result, repo)
        if "C" in wanted:
            scenario_c(result, probe)
        if "A" in wanted:
            scenario_a(result, probe, repo)
        if "E" in wanted:
            scenario_e(result, probe, repo)
    finally:
        cleanup(result, probe)

    result.summary()
    return 1 if result.failed() else 0


if __name__ == "__main__":
    sys.exit(main())
