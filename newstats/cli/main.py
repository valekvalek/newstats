from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import typer

from newstats.db.session import SessionLocal
from newstats.importer.flashscore import (
    ingest_event,
    ingest_match_events,
    ingest_match_stats,
    ingest_odds,
    ingest_standings,
)
from newstats.stats.form import form_summary
from newstats.stats.head_to_head import head_to_head

app = typer.Typer(
    help=(
        "Own stats DB + betting-analysis stats, fed by piping "
        "M3MONs/FlashscoreScraper's JSON output in."
    )
)


def _read_json(file: Path | None) -> dict:
    raw = file.read_text() if file else sys.stdin.read()
    return json.loads(raw)


@app.command()
def ingest_event_cmd(
    url: str = typer.Argument(..., help="The match URL you ran `event` against"),
    file: Path = typer.Option(None, help="JSON file; defaults to stdin"),  # noqa: B008
    sport: str = typer.Option("football", help="Sport slug"),
):
    """Ingest the output of: python main.py event <url>"""
    session = SessionLocal()
    try:
        match = ingest_event(session, url, _read_json(file), sport_name=sport)
        typer.echo(f"match id={match.id} external_id={match.external_id} status={match.status.value}")
    finally:
        session.close()


@app.command()
def ingest_odds_cmd(
    file: Path = typer.Option(None, help="JSON file; defaults to stdin"),  # noqa: B008
    scraped_at: str = typer.Option(
        None, help="ISO timestamp for when this scrape ran; defaults to now"
    ),
):
    """Ingest the output of: python main.py odds <url> (one call per odds type)."""
    when = dt.datetime.fromisoformat(scraped_at) if scraped_at else dt.datetime.now(dt.UTC)
    session = SessionLocal()
    try:
        rows = ingest_odds(session, _read_json(file), when)
        typer.echo(f"ingested {len(rows)} odds row(s)")
    finally:
        session.close()


@app.command()
def ingest_standings_cmd(
    url: str = typer.Argument(..., help="The match URL the standings page belongs to"),
    file: Path = typer.Option(None, help="JSON file; defaults to stdin"),  # noqa: B008
    scraped_at: str = typer.Option(None, help="ISO timestamp; defaults to now"),
):
    """Ingest the output of: python main.py event-info <url>/standings/standings/overall/"""
    when = dt.datetime.fromisoformat(scraped_at) if scraped_at else dt.datetime.now(dt.UTC)
    session = SessionLocal()
    try:
        rows = ingest_standings(session, url, _read_json(file), when)
        typer.echo(f"ingested {len(rows)} standings row(s)")
    finally:
        session.close()


@app.command()
def ingest_stats_cmd(
    url: str = typer.Argument(..., help="The match URL this team-stats scrape belongs to"),
    file: Path = typer.Option(None, help="JSON file; defaults to stdin"),  # noqa: B008
):
    """
    Ingest team-level match stats (corners, fouls, possession, ...).

    Not something FlashscoreScraper's CLI exposes -- this expects
    {"stats": [{"label": "Corner kicks", "home": "2", "away": "5"}, ...]}
    as produced by scraping a match's own ".../summary/stats/overall/"
    page directly (see newstats/importer/flashscore.py's module docstring).
    """
    session = SessionLocal()
    try:
        payload = _read_json(file)
        rows = ingest_match_stats(session, url, payload.get("stats") or [])
        typer.echo(f"ingested {len(rows)} stat row(s)")
    finally:
        session.close()


@app.command()
def ingest_events_cmd(
    url: str = typer.Argument(..., help="The match URL this incident-timeline scrape belongs to"),
    file: Path = typer.Option(None, help="JSON file; defaults to stdin"),  # noqa: B008
):
    """
    Ingest player events (goals, assists, cards) from a match's incident timeline.

    Not something FlashscoreScraper's CLI exposes -- this expects
    {"incidents": [{"type": "goal", "side": "home", "player_name": "...",
    "player_link": "/player/.../..."}, ...]} as produced by scraping a
    match's own summary page directly (see newstats/importer/flashscore.py's
    module docstring).
    """
    session = SessionLocal()
    try:
        payload = _read_json(file)
        rows = ingest_match_events(session, url, payload.get("incidents") or [])
        typer.echo(f"ingested {len(rows)} player-stat row(s)")
    finally:
        session.close()


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
