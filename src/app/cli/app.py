import shutil
import sys
from importlib.metadata import version as _package_version
from pathlib import Path
from typing import Annotated
import typer

from app.cli import autostart, daemon
from utils.helper import get_db_url, is_packaged_install
from vcs.db.sqlite import DBHandler
from vcs.services.configure import add_sources, remove_sources, health_check
from vcs.services.audit import (
    InvalidSessionActorError,
    OutOfScopeError,
    check_diff,
    get_sources,
    get_version_list,
    rollback_session,
    rollback_source,
)

cli = typer.Typer(name = "ctx")
sources_cli = typer.Typer()
daemon_cli = typer.Typer()

cli.add_typer(sources_cli, name="source")
cli.add_typer(daemon_cli, name="daemon")


def _print_version(value: bool):
    if value:
        typer.echo(_package_version("chrono-ctx"))
        raise typer.Exit()


def _path_not_found_hint(script_dir: Path, platform: str) -> str:
    if platform == "win32":
        return (
            f"ctx is not on PATH. Add it for this user:\n"
            f'  setx PATH "%PATH%;{script_dir}"\n'
            f"(open a new shell afterward for this to take effect)"
        )
    return (
        f"ctx is not on PATH. Add it (e.g. append to ~/.bashrc or ~/.zshrc "
        f"to persist it):\n"
        f'  export PATH="$PATH:{script_dir}"'
    )


def _warn_if_not_on_path():
    # Source checkouts run via `uv run ctx`, which resolves correctly by
    # construction - this check would just be noise for contributors.
    if not is_packaged_install():
        return
    if shutil.which("ctx") is not None:
        return
    script_dir = Path(sys.argv[0]).resolve().parent
    typer.echo(_path_not_found_hint(script_dir, sys.platform), err=True)


@cli.callback()
def main(
    version: Annotated[
        bool,
        typer.Option("--version", "-V", is_eager=True, callback=_print_version, help="Show the installed version and exit."),
    ] = False,
):
    _warn_if_not_on_path()

#Path completion
def complete_path(incomplete: str):
    p = Path(incomplete or ".")
    parent = p.parent if p.parent != Path("") else Path(".")

    for child in parent.iterdir():
        if child.name.startswith(p.name):
            yield str(child)

PathArg = typer.Argument(..., shell_complete=complete_path)

#Health check
@cli.command("health")
def health():
    health_check()

#Configuration
@sources_cli.command("add")
def sources_add(paths: list[Path] = PathArg):
    add_sources(paths)
    for path in paths:
        typer.echo(f"added: {path}")

@sources_cli.command("remove")
def sources_remove(paths: list[Path] = PathArg):
    remove_sources(paths)
    for path in paths:
        typer.echo(f"removed: {path}")

@sources_cli.command("list")
def sources():
    db_handler = DBHandler.from_url(get_db_url())
    try:
        for source in get_sources(db_handler):
            version = source["version"] or "-"
            typer.echo(f"{source['location']}  [{source['provider']}]  status={source['status']}  version={version}")
    finally:
        db_handler.close()

#Audit
@cli.command("history")
def get_version_history(path: Path = PathArg):
    try:
        versions = get_version_list(str(path))
    except OutOfScopeError:
        typer.echo(f"{path}: out of scope", err=True)
        raise typer.Exit(code=1)

    if not versions:
        typer.echo("no history")
        return
    for version in versions:
        typer.echo(f"{version['rev']}  {version['timestamp']}  {version['author']}  {version['message']}")

@cli.command("rollback")
def rollback(
    path: Path = PathArg,
    version: Annotated[
        str,
        typer.Option("--version", "-v", help="Git rev to roll back to"),
    ] = ...,
):
    try:
        new_rev = rollback_source(str(path), version)
    except OutOfScopeError:
        typer.echo(f"{path}: out of scope", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"rolled back {path} to {version}, new version {new_rev}")

@cli.command("rollback-session")
def rollback_session_cmd(
    actor_label: Annotated[
        str,
        typer.Argument(help="Actor label from ctx history's author column, e.g. agent:<session_id>"),
    ],
):
    try:
        result = rollback_session(actor_label)
    except InvalidSessionActorError as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)

    for path in result["rolled_back"]:
        typer.echo(f"rolled back: {path}")
    for failure in result["failed"]:
        typer.echo(f"failed: {failure['path']}: {failure['error']}", err=True)
    if result["failed"]:
        raise typer.Exit(code=1)

@cli.command("diff")
def show_diff(
    path: Path = PathArg,
    from_rev: Annotated[
        str | None,
        typer.Option("--from", help="Git rev to diff from"),
    ] = None,
    to_rev: Annotated[
        str | None,
        typer.Option("--to", help="Git rev to diff to"),
    ] = None,
):
    if (from_rev is None) != (to_rev is None):
        typer.echo("--from and --to must be given together", err=True)
        raise typer.Exit(code=1)

    try:
        if from_rev is None:
            versions = get_version_list(str(path))
            if len(versions) < 2:
                typer.echo("not enough history to diff (need at least 2 versions)", err=True)
                raise typer.Exit(code=1)
            to_rev = versions[0]["rev"]
            from_rev = versions[1]["rev"]
        text = check_diff(str(path), from_rev, to_rev)
    except OutOfScopeError:
        typer.echo(f"{path}: out of scope", err=True)
        raise typer.Exit(code=1)

    typer.echo(text or "no changes")


#Daemon
@daemon_cli.command("start")
def daemon_start():
    try:
        pid = daemon.start()
    except daemon.DaemonAlreadyRunningError as e:
        typer.echo(f"daemon already running (pid {e.pid})", err=True)
        raise typer.Exit(code=1)
    typer.echo(f"daemon started, pid {pid}")

@daemon_cli.command("stop")
def daemon_stop():
    stopped = daemon.stop()
    typer.echo("daemon stopped" if stopped else "daemon not running")

@daemon_cli.command("status")
def daemon_status():
    result = daemon.status()
    if result["running"]:
        typer.echo(f"running, pid {result['pid']}")
    else:
        typer.echo("stopped")

@daemon_cli.command("enable")
def daemon_enable():
    try:
        result = autostart.enable()
    except (NotImplementedError, autostart.AutostartError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(result)

@daemon_cli.command("disable")
def daemon_disable():
    try:
        result = autostart.disable()
    except (NotImplementedError, autostart.AutostartError) as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(code=1)
    typer.echo(result)


if __name__ == "__main__":
    cli(prog_name="ctx")
