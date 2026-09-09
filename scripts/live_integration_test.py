#!/usr/bin/env python3
"""Live integration test automation.

Automates the manual pass documented in
docs/agents/draft/live-integration-test-plan.md ("Run log - 2026-09-09") -
runs against the REAL configured daemon and REAL config.yaml, not a mock or
a pytest fixture. Exercises, in order:

  A. Daemon lifecycle (start/status).
  C. MCP tool calls through a real fastmcp.Client: create_file, read_file,
     write_file, optimistic-concurrency conflict detection.
  C. Out-of-scope create_file -> elicitation -> approval -> scope grant
     persisted to config.yaml (skipped if no safe out-of-scope probe path
     can be found - see --out-of-scope-file).
  E. Cross-process actor attribution (real git log) + `ctx rollback-session`
     run as a separate OS process.
  F. HTTP API auth gate (401/401/200) against a real uvicorn process.

Cleans up everything it creates: probe files are deleted for real (through
the live watcher, so the mirror repo reflects it), any auto-granted scope is
reverted via `ctx source remove`, config.yaml is restored to its prior
state. Safe to re-run.

NOT covered, by construction (see the plan doc's step B): whether a real
Claude Code client actually renders the MCP guardrail's elicitation prompt.
This script's elicitation handler auto-approves in-process - it proves the
server-side mechanism, not what any particular MCP client's UI does. That
needs a live Claude Code session.

Known issues this script's pacing/fallbacks were built against (see
docs/agents/issues.md) - #25, #27 and #28 are now fixed (specs 022/023/024);
kept here because the sleeps/fallback still do no harm and this script is
what caught #27/#28 in the first place:
  #25 - init_repo() had no cross-process lock; calling back into the MCP
        server immediately after a write could race the daemon's own
        commit. The short sleeps at those points stay - the fix narrows the
        race, harmless margin either way.
  #26 - still OPEN: the daemon has been observed to die with no log trace;
        this script does not attempt to detect or recover from that, only
        reports the daemon's status at each checkpoint.
  #27 - `ctx daemon stop` used to always raise OSError on Windows.
        stop_daemon()'s force-kill fallback on OSError stays as a safety
        net in case of a regression or an unhandled Windows variant.
  #28 - `rollback_session` used to crash when an actor's earliest commit on
        a path was that path's first-ever commit in a mirror repo that
        already had unrelated history. E.rollback_session should PASS now;
        cleanup() still force-deletes the probe file regardless of the
        rollback outcome either way.

Usage:
    uv run python scripts/live_integration_test.py
    uv run python scripts/live_integration_test.py --keep-daemon --http-port 8010

Requires `git` and `uv` on PATH, and config.yaml with at least one `local`
source already configured.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from fastmcp import Client  # noqa: E402
from fastmcp.client.elicitation import ElicitResult  # noqa: E402

import app.mcp.server as server  # noqa: E402
from vcs.services import mirror_path  # noqa: E402
from vcs.services.configure import derive_watch_targets, is_path_in_scope, parse_config  # noqa: E402

CONFIG_PATH = PROJECT_ROOT / "config.yaml"


@dataclass
class Result:
    steps: list[tuple[str, str, str]] = field(default_factory=list)  # (id, status, detail)

    def record(self, step_id: str, status: str, detail: str = "") -> None:
        self.steps.append((step_id, status, detail))
        print(f"[{status}] {step_id} {detail}")

    def failed(self) -> bool:
        return any(status == "FAIL" for _, status, _ in self.steps)

    def summary(self) -> None:
        print("\n=== Live integration test summary ===")
        for step_id, status, detail in self.steps:
            print(f"  [{status:4}] {step_id} {detail}")


def run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(PROJECT_ROOT), capture_output=True, text=True, **kwargs)


def start_daemon_if_needed(result: Result) -> bool:
    import app.cli.daemon as daemon_mod

    status = daemon_mod.status()
    if status["running"]:
        result.record("A.daemon", "PASS", f"already running, pid {status['pid']}")
        return False
    new_pid = daemon_mod.start()
    time.sleep(3)
    status = daemon_mod.status()
    if not status["running"]:
        result.record("A.daemon", "FAIL", f"start returned pid {new_pid}, but status is not running")
        raise SystemExit(1)
    result.record("A.daemon", "PASS", f"running, pid {status['pid']}")
    return True


def stop_daemon(result: Result) -> None:
    import app.cli.daemon as daemon_mod

    pid = daemon_mod._read_pid()
    try:
        daemon_mod.stop()
    except OSError as exc:
        # issue #27: _send_stop_signal's os.kill(pid, CTRL_BREAK_EVENT) always raises
        # OSError on Windows against a DETACHED_PROCESS child (WinError 87) - stop()'s
        # own force-kill fallback never runs because the crash happens before that
        # code path. Do here what stop() would have done after a timeout.
        if pid is not None:
            daemon_mod._force_kill(pid)
        daemon_mod.PID_PATH.unlink(missing_ok=True)
        result.record("teardown.daemon", "PASS", f"stop() raised {exc!r} (issue #27) - force-killed pid {pid} instead")
        return

    status = daemon_mod.status()
    result.record("teardown.daemon", "PASS" if not status["running"] else "FAIL", status)


def pick_probe_paths(result: Result, out_of_scope_override: str | None) -> tuple[Path, Path | None]:
    config = parse_config()
    local_sources = [s for s in config["sources"] if s.get("type") == "local"]
    if not local_sources:
        result.record("setup.probe_paths", "FAIL", "config.yaml has no local sources")
        raise SystemExit(1)

    in_scope_dir = Path(local_sources[0]["path"])
    in_scope_probe = in_scope_dir / "live-test-probe.md"
    result.record("setup.probe_paths", "PASS", f"in-scope={in_scope_probe}")

    if out_of_scope_override:
        return in_scope_probe, Path(out_of_scope_override)

    parent = in_scope_dir.parent
    configured = {Path(s["path"]).resolve() for s in local_sources}
    for sibling in sorted(p for p in parent.iterdir() if p.is_dir()):
        if sibling.resolve() not in configured and not is_path_in_scope(str(sibling / "x")):
            out_of_scope_probe = sibling / "live-test-probe.txt"
            result.record("setup.out_of_scope_path", "PASS", str(out_of_scope_probe))
            return in_scope_probe, out_of_scope_probe

    result.record("setup.out_of_scope_path", "SKIP", "no safe out-of-scope sibling found under " + str(parent))
    return in_scope_probe, None


async def _approve(message, response_type, params, ctx):
    return ElicitResult(action="accept", content=True)


async def run_mcp_checks(result: Result, in_scope_probe: Path, out_of_scope_probe: Path | None) -> None:
    async with Client(server.mcp, elicitation_handler=_approve) as client:
        r = await client.call_tool("create_file", {"path": str(in_scope_probe), "content": "probe v1"})
        ok = r.data.get("status") == "ok"
        result.record("C.create_file", "PASS" if ok else "FAIL", str(r.data))

        # issue #25: init_repo() races unlocked against the daemon's own commit of
        # this same fresh file - give the daemon's debounce+commit a moment first.
        await asyncio.sleep(2)

        r = await client.call_tool("read_file", {"path": str(in_scope_probe)})
        v1 = r.data.get("version")
        result.record("C.read_file", "PASS" if r.data.get("status") == "ok" and v1 else "FAIL", str(r.data))

        r = await client.call_tool(
            "write_file", {"path": str(in_scope_probe), "content": "probe v2", "expected_version": v1}
        )
        ok = r.data.get("status") == "ok"
        result.record("C.write_file.correct_version", "PASS" if ok else "FAIL", str(r.data))

        # Write commits are async (watcher-driven, spec 021's documented limit) -
        # wait for the commit to actually land before treating v1 as genuinely stale.
        await asyncio.sleep(2)

        r = await client.call_tool(
            "write_file", {"path": str(in_scope_probe), "content": "probe v3", "expected_version": v1}
        )
        ok = r.data.get("status") == "conflict"
        result.record("D.optimistic_concurrency_conflict", "PASS" if ok else "FAIL", str(r.data))

        if out_of_scope_probe is not None:
            r = await client.call_tool(
                "create_file", {"path": str(out_of_scope_probe), "content": "skills probe"}
            )
            ok = r.data.get("status") == "ok"
            granted = is_path_in_scope(str(out_of_scope_probe))
            status = "PASS" if ok and granted else "FAIL"
            result.record("C.elicitation_and_scope_grant", status, str(r.data))
        else:
            result.record("C.elicitation_and_scope_grant", "SKIP", "no out-of-scope probe path")


def latest_commit_author(in_scope_probe: Path) -> str | None:
    watch_targets = derive_watch_targets()
    repo_path, relpath = mirror_path.resolve_mirror_location(str(in_scope_probe), watch_targets)
    r = subprocess.run(
        ["git", "log", "-1", "--format=%an", "--", relpath],
        cwd=str(repo_path), capture_output=True, text=True,
    )
    author = r.stdout.strip()
    return author or None


def run_rollback(result: Result, actor_label: str | None) -> None:
    if not actor_label:
        result.record("E.rollback_session", "SKIP", "no actor label recovered from git log")
        return
    r = run(["uv", "run", "ctx", "rollback-session", actor_label])
    ok = r.returncode == 0 and "live-test-probe.md" in r.stdout
    result.record("E.rollback_session", "PASS" if ok else "FAIL", r.stdout.strip() or r.stderr.strip())


def cleanup(result: Result, in_scope_probe: Path, out_of_scope_probe: Path | None) -> None:
    for probe in (in_scope_probe, out_of_scope_probe):
        if probe is not None and probe.exists():
            probe.unlink()
    time.sleep(3)  # let the live watcher pick up the deletes before we check state

    if out_of_scope_probe is not None:
        run(["uv", "run", "ctx", "source", "remove", str(out_of_scope_probe)])

    r = run(["git", "status", "--short", "--", str(in_scope_probe.parent)])
    clean = r.stdout.strip() == ""
    result.record("cleanup.tracked_tree_clean", "PASS" if clean else "FAIL", r.stdout.strip())


def http_api_check(result: Result, port: int) -> None:
    # python -m app.api.server hardcodes port 8000 (no --port support) - go through
    # uvicorn directly so this check can use a throwaway port instead of colliding
    # with anything a developer already has bound to 8000. sys.executable -m uvicorn
    # (not `uv run uvicorn ...`) so this Popen handle IS the uvicorn process, not a
    # wrapper around it - `uv run` as an extra layer left orphaned uvicorn processes
    # behind on every run, since proc.terminate() only reached the `uv` wrapper.
    env = {**os.environ, "HTTP_API_KEY": "live-test-key"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.api.server:app", "--port", str(port)],
        cwd=str(PROJECT_ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}/v1/sources"
    try:
        for _ in range(20):
            try:
                requests.get(base, timeout=1)
                break
            except requests.exceptions.ConnectionError:
                time.sleep(0.5)
        else:
            result.record("F.http_api", "FAIL", "server never came up")
            return

        no_key = requests.get(base, timeout=5).status_code
        wrong_key = requests.get(base, headers={"X-API-Key": "nope"}, timeout=5).status_code
        correct_key = requests.get(base, headers={"X-API-Key": "live-test-key"}, timeout=5).status_code
        ok = (no_key, wrong_key, correct_key) == (401, 401, 200)
        result.record(
            "F.http_api", "PASS" if ok else "FAIL",
            f"no_key={no_key} wrong_key={wrong_key} correct_key={correct_key}",
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--keep-daemon", action="store_true", help="don't stop the daemon if this script started it")
    parser.add_argument("--http-port", type=int, default=8010, help="port for the throwaway HTTP API check")
    parser.add_argument("--out-of-scope-file", default=None, help="explicit out-of-scope probe path (skips auto-detection)")
    args = parser.parse_args()

    if not CONFIG_PATH.exists():
        print(f"no config.yaml at {CONFIG_PATH} - nothing to test against", file=sys.stderr)
        return 1

    result = Result()
    started_daemon = start_daemon_if_needed(result)
    in_scope_probe, out_of_scope_probe = pick_probe_paths(result, args.out_of_scope_file)

    try:
        asyncio.run(run_mcp_checks(result, in_scope_probe, out_of_scope_probe))
        actor = latest_commit_author(in_scope_probe)
        run_rollback(result, actor)
        http_api_check(result, args.http_port)
    finally:
        cleanup(result, in_scope_probe, out_of_scope_probe)
        if started_daemon and not args.keep_daemon:
            stop_daemon(result)

    result.record(
        "B.claude_code_elicitation_render", "SKIP",
        "requires a real Claude Code session - not answerable from this script",
    )
    result.summary()
    return 1 if result.failed() else 0


if __name__ == "__main__":
    raise SystemExit(main())
