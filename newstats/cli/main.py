from __future__ import annotations

import os
from pathlib import Path

import typer

from newstats.db.session import SessionLocal
from newstats.importer.loader import load_directory
from newstats.stats.form import form_summary
from newstats.stats.head_to_head import head_to_head

app = typer.Typer(help="Own stats DB + analysis, fed by an external scraper's JSON output.")


@app.command()
def import_data(
    directory: Path = typer.Option(  # noqa: B008  (standard Typer idiom)
        None, help="Defaults to $SCRAPER_OUTPUT_DIR from .env"
    )
):
    """Import every *.json fixture the scraper has written into our DB."""
    target = directory or Path(os.environ.get("SCRAPER_OUTPUT_DIR", "./data/scraper_output"))
    if not target.exists():
        typer.echo(f"No such directory: {target}")
        raise typer.Exit(1)

    session = SessionLocal()
    try:
        count = load_directory(session, target)
    finally:
        session.close()
    typer.echo(f"Imported {count} match file(s) from {target}")


@app.command()
def form(team_id: int, limit: int = 10):
    """Recent-form summary for a team."""
    session = SessionLocal()
    try:
        typer.echo(form_summary(session, team_id, limit))
    finally:
        session.close()


@app.command()
def h2h(team_a_id: int, team_b_id: int, limit: int = 20):
    """Head-to-head record between two teams."""
    session = SessionLocal()
    try:
        typer.echo(head_to_head(session, team_a_id, team_b_id, limit))
    finally:
        session.close()


if __name__ == "__main__":
    app()
