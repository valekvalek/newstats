from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from newstats.db.models import Match, MatchStatus


def head_to_head(session: Session, team_a_id: int, team_b_id: int, limit: int = 20) -> dict:
    stmt = (
        select(Match)
        .where(
            Match.status == MatchStatus.FINISHED,
            (
                ((Match.home_team_id == team_a_id) & (Match.away_team_id == team_b_id))
                | ((Match.home_team_id == team_b_id) & (Match.away_team_id == team_a_id))
            ),
        )
        .order_by(Match.kickoff_at.desc())
        .limit(limit)
    )
    matches = list(session.execute(stmt).scalars())

    team_a_wins = team_b_wins = draws = 0
    for match in matches:
        if match.home_score is None or match.away_score is None:
            continue
        a_is_home = match.home_team_id == team_a_id
        a_score = match.home_score if a_is_home else match.away_score
        b_score = match.away_score if a_is_home else match.home_score
        if a_score > b_score:
            team_a_wins += 1
        elif a_score < b_score:
            team_b_wins += 1
        else:
            draws += 1

    return {
        "matches_considered": len(matches),
        "team_a_wins": team_a_wins,
        "team_b_wins": team_b_wins,
        "draws": draws,
    }
