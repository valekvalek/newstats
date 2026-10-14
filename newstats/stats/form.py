"""Recent-form stats for a team — the first thing any betting model needs."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from newstats.db.models import Match, MatchStatus


def recent_matches(session: Session, team_id: int, limit: int = 10) -> list[Match]:
    stmt = (
        select(Match)
        .where(
            Match.status == MatchStatus.FINISHED,
            (Match.home_team_id == team_id) | (Match.away_team_id == team_id),
        )
        .order_by(Match.kickoff_at.desc())
        .limit(limit)
    )
    return list(session.execute(stmt).scalars())


def form_summary(session: Session, team_id: int, limit: int = 10) -> dict:
    """Wins/draws/losses/goals for the last `limit` finished matches."""
    matches = recent_matches(session, team_id, limit)
    wins = draws = losses = goals_for = goals_against = 0

    for match in matches:
        is_home = match.home_team_id == team_id
        gf = match.home_score if is_home else match.away_score
        ga = match.away_score if is_home else match.home_score
        if gf is None or ga is None:
            continue
        goals_for += gf
        goals_against += ga
        if gf > ga:
            wins += 1
        elif gf == ga:
            draws += 1
        else:
            losses += 1

    return {
        "matches_considered": len(matches),
        "wins": wins,
        "draws": draws,
        "losses": losses,
        "goals_for": goals_for,
        "goals_against": goals_against,
    }
