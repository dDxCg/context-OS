from pathlib import Path
from typing import Annotated
import typer

from utils.helper import get_db_url
from vcs.db.sqlite import DBHandler
from vcs.services.configure import add_sources, remove_sources, health_check
from vcs.services.audit import (
    OutOfScopeError,
    check_diff,
    get_sources,
    get_version_list,
    rollback_source,
)

cli = typer.Typer(name = "ctx")
sources_cli = typer.Typer()

cli.add_typer(sources_cli, name="source")

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


if __name__ == "__main__":
    cli(prog_name="ctx")
