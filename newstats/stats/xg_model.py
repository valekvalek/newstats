"""
Simple xG-based Poisson goal model + market comparison.

Not Dixon-Coles, not a fitted regression -- deliberately the simplest model
that uses real data: each team's average xG for/against (from
`match_stats`, stat_name="expected_goals_xg") scaled against the league
average, with one empirical home-advantage factor computed from the same
data (how much bigger a home team's average xG is than an away team's, in
the matches we actually have). Good enough to rank matches by edge against
market odds; not a substitute for a properly fitted model once there's a
full season of data to fit one against.

If a team has no xG-carrying matches in the DB at all, `team_xg_strength`
returns None rather than guessing -- callers decide whether to fall back
(e.g. to actual goals) or skip the match.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from newstats.db.models import Match, MatchStat

_XG_STAT = "expected_goals_xg"
_MAX_GOALS = 14  # Poisson grid cutoff; negligible tail mass even for a strong favourite's lambda.


def league_average_xg(session: Session) -> float:
    avg = session.query(func.avg(MatchStat.value)).filter(MatchStat.stat_name == _XG_STAT).scalar()
    return float(avg) if avg is not None else 1.35  # ~global average goals/team/match fallback


def home_advantage_factor(session: Session) -> float:
    """avg home-team xG / avg away-team xG across matches with xG on both sides."""
    home_avg = (
        session.query(func.avg(MatchStat.value))
        .join(Match, Match.id == MatchStat.match_id)
        .filter(MatchStat.stat_name == _XG_STAT, MatchStat.team_id == Match.home_team_id)
        .scalar()
    )
    away_avg = (
        session.query(func.avg(MatchStat.value))
        .join(Match, Match.id == MatchStat.match_id)
        .filter(MatchStat.stat_name == _XG_STAT, MatchStat.team_id == Match.away_team_id)
        .scalar()
    )
    if not home_avg or not away_avg:
        return 1.15  # commonly-cited football home-advantage ballpark
    return float(home_avg) / float(away_avg)


@dataclass
class TeamXgStrength:
    team_id: int
    xg_for_avg: float
    xg_against_avg: float
    matches_considered: int


def team_xg_strength(session: Session, team_id: int, limit: int = 10) -> TeamXgStrength | None:
    """Average xG for/against over the team's most recent xG-carrying matches."""
    for_rows = (
        session.query(MatchStat.value)
        .join(Match, Match.id == MatchStat.match_id)
        .filter(MatchStat.stat_name == _XG_STAT, MatchStat.team_id == team_id)
        .order_by(Match.kickoff_at.desc())
        .limit(limit)
        .all()
    )
    if not for_rows:
        return None

    against_rows = (
        session.query(MatchStat.value)
        .join(Match, Match.id == MatchStat.match_id)
        .filter(
            MatchStat.stat_name == _XG_STAT,
            MatchStat.team_id != team_id,
            (Match.home_team_id == team_id) | (Match.away_team_id == team_id),
        )
        .order_by(Match.kickoff_at.desc())
        .limit(limit)
        .all()
    )

    xg_for = [float(v[0]) for v in for_rows]
    xg_against = [float(v[0]) for v in against_rows]
    return TeamXgStrength(
        team_id=team_id,
        xg_for_avg=sum(xg_for) / len(xg_for),
        xg_against_avg=sum(xg_against) / len(xg_against) if xg_against else league_average_xg(session),
        matches_considered=len(xg_for),
    )


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


@dataclass
class MatchPrediction:
    home_team_id: int
    away_team_id: int
    lambda_home: float
    lambda_away: float
    p_home_win: float
    p_draw: float
    p_away_win: float
    p_over_2_5: float
    p_btts_yes: float
    most_likely_score: tuple[int, int]


def predict_match(session: Session, home_team_id: int, away_team_id: int) -> MatchPrediction | None:
    home = team_xg_strength(session, home_team_id)
    away = team_xg_strength(session, away_team_id)
    if home is None or away is None:
        return None

    league_avg = league_average_xg(session)
    home_adv = home_advantage_factor(session)

    attack_home = home.xg_for_avg / league_avg
    defense_away = away.xg_against_avg / league_avg
    attack_away = away.xg_for_avg / league_avg
    defense_home = home.xg_against_avg / league_avg

    lambda_home = league_avg * attack_home * defense_away * home_adv
    lambda_away = league_avg * attack_away * defense_home

    grid = [[_poisson_pmf(h, lambda_home) * _poisson_pmf(a, lambda_away) for a in range(_MAX_GOALS + 1)] for h in range(_MAX_GOALS + 1)]

    p_home_win = sum(grid[h][a] for h in range(_MAX_GOALS + 1) for a in range(_MAX_GOALS + 1) if h > a)
    p_draw = sum(grid[h][a] for h in range(_MAX_GOALS + 1) for a in range(_MAX_GOALS + 1) if h == a)
    p_away_win = sum(grid[h][a] for h in range(_MAX_GOALS + 1) for a in range(_MAX_GOALS + 1) if h < a)
    p_over_2_5 = sum(grid[h][a] for h in range(_MAX_GOALS + 1) for a in range(_MAX_GOALS + 1) if h + a > 2.5)
    p_btts_yes = sum(grid[h][a] for h in range(_MAX_GOALS + 1) for a in range(_MAX_GOALS + 1) if h > 0 and a > 0)

    most_likely = max(
        ((h, a) for h in range(_MAX_GOALS + 1) for a in range(_MAX_GOALS + 1)),
        key=lambda ha: grid[ha[0]][ha[1]],
    )

    return MatchPrediction(
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        lambda_home=lambda_home,
        lambda_away=lambda_away,
        p_home_win=p_home_win,
        p_draw=p_draw,
        p_away_win=p_away_win,
        p_over_2_5=p_over_2_5,
        p_btts_yes=p_btts_yes,
        most_likely_score=most_likely,
    )


def devig_1x2(odds_home: float, odds_draw: float, odds_away: float) -> tuple[float, float, float]:
    """Decimal odds -> de-vigged implied probabilities (bookmaker margin removed by normalizing)."""
    raw = (1 / odds_home, 1 / odds_draw, 1 / odds_away)
    total = sum(raw)
    return (raw[0] / total, raw[1] / total, raw[2] / total)
