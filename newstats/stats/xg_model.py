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

import datetime as dt
import math
from dataclasses import dataclass

from sqlalchemy import func
from sqlalchemy.orm import Session

from newstats.db.models import Match, MatchStat

_XG_STAT = "expected_goals_xg"
_MAX_GOALS = 14  # Poisson grid cutoff; negligible tail mass even for a strong favourite's lambda.


def league_average_xg(session: Session, before: dt.datetime | None = None) -> float:
    q = session.query(func.avg(MatchStat.value)).filter(MatchStat.stat_name == _XG_STAT)
    if before is not None:
        q = q.join(Match, Match.id == MatchStat.match_id).filter(Match.kickoff_at < before)
    avg = q.scalar()
    return float(avg) if avg is not None else 1.35  # ~global average goals/team/match fallback


def home_advantage_factor(session: Session, before: dt.datetime | None = None) -> float:
    """avg home-team xG / avg away-team xG across matches with xG on both sides."""

    def _side_avg(side_col):
        q = (
            session.query(func.avg(MatchStat.value))
            .join(Match, Match.id == MatchStat.match_id)
            .filter(MatchStat.stat_name == _XG_STAT, MatchStat.team_id == side_col)
        )
        if before is not None:
            q = q.filter(Match.kickoff_at < before)
        return q.scalar()

    home_avg = _side_avg(Match.home_team_id)
    away_avg = _side_avg(Match.away_team_id)
    if not home_avg or not away_avg:
        return 1.15  # commonly-cited football home-advantage ballpark
    return float(home_avg) / float(away_avg)


@dataclass
class TeamXgStrength:
    team_id: int
    xg_for_avg: float
    xg_against_avg: float
    matches_considered: int


def team_xg_strength(
    session: Session, team_id: int, limit: int = 10, before: dt.datetime | None = None
) -> TeamXgStrength | None:
    """
    Average xG for/against over the team's most recent xG-carrying matches.

    `before`, when given, restricts to matches with kickoff_at strictly
    before that timestamp -- pass the match being predicted's own
    kickoff_at when backtesting, or the model would be "predicting" a match
    using that same match's own xG (and any later matches already in the
    DB) as part of the team's history. Without it this is only safe to call
    for a genuinely upcoming match that has no xG of its own yet.
    """
    for_q = (
        session.query(MatchStat.value)
        .join(Match, Match.id == MatchStat.match_id)
        .filter(MatchStat.stat_name == _XG_STAT, MatchStat.team_id == team_id)
    )
    against_q = (
        session.query(MatchStat.value)
        .join(Match, Match.id == MatchStat.match_id)
        .filter(
            MatchStat.stat_name == _XG_STAT,
            MatchStat.team_id != team_id,
            (Match.home_team_id == team_id) | (Match.away_team_id == team_id),
        )
    )
    if before is not None:
        for_q = for_q.filter(Match.kickoff_at < before)
        against_q = against_q.filter(Match.kickoff_at < before)

    for_rows = for_q.order_by(Match.kickoff_at.desc()).limit(limit).all()
    if not for_rows:
        return None
    against_rows = against_q.order_by(Match.kickoff_at.desc()).limit(limit).all()

    xg_for = [float(v[0]) for v in for_rows]
    xg_against = [float(v[0]) for v in against_rows]
    return TeamXgStrength(
        team_id=team_id,
        xg_for_avg=sum(xg_for) / len(xg_for),
        xg_against_avg=sum(xg_against) / len(xg_against) if xg_against else league_average_xg(session, before=before),
        matches_considered=len(xg_for),
    )


#: Shrinkage strength for team attack/defense ratings: w = n / (n + K). Fixed
#: before looking at backtest results, not tuned against them (see README
#: "Результаты бэктеста"). A team with n=K matches gets 50% weight on its own
#: average and 50% on the league average; n=1 is mostly league-average.
_SHRINKAGE_K = 5.0


def shrunk_attack_defense(strength: TeamXgStrength, league_avg: float, k: float = _SHRINKAGE_K) -> tuple[float, float]:
    """
    (attack, defense) ratios, already normalized by league_avg (1.0 = average),
    shrunk toward 1.0 by sample size -- a team with only 1-2 xG-carrying
    matches shouldn't be treated as confidently as one with 10. Without this,
    team_xg_strength's matches_considered was computed but never used.
    """
    w = strength.matches_considered / (strength.matches_considered + k)
    attack = (w * strength.xg_for_avg + (1 - w) * league_avg) / league_avg
    defense = (w * strength.xg_against_avg + (1 - w) * league_avg) / league_avg
    return attack, defense


def _poisson_pmf(k: int, lam: float) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


@dataclass(frozen=True)
class _XgRow:
    home_id: int
    away_id: int
    home_xg: float
    away_xg: float
    kickoff_at: dt.datetime


def _load_xg_rows(session: Session, before: dt.datetime | None) -> list[_XgRow]:
    match_q = session.query(Match.id, Match.home_team_id, Match.away_team_id, Match.kickoff_at).filter(Match.kickoff_at.isnot(None))
    if before is not None:
        match_q = match_q.filter(Match.kickoff_at < before)
    matches = {m_id: (home_id, away_id, kickoff_at) for m_id, home_id, away_id, kickoff_at in match_q}
    if not matches:
        return []

    stat_q = session.query(MatchStat.match_id, MatchStat.team_id, MatchStat.value).filter(
        MatchStat.stat_name == _XG_STAT, MatchStat.match_id.in_(matches.keys())
    )
    xg_by_match: dict[int, dict[int, float]] = {}
    for match_id, team_id, value in stat_q:
        xg_by_match.setdefault(match_id, {})[team_id] = float(value)

    rows: list[_XgRow] = []
    for match_id, (home_id, away_id, kickoff_at) in matches.items():
        by_team = xg_by_match.get(match_id, {})
        if home_id in by_team and away_id in by_team:
            rows.append(_XgRow(home_id, away_id, by_team[home_id], by_team[away_id], kickoff_at))
    return rows


#: Fitted-ratings cache, keyed by (before, xi): computing a_i/d_i is an O(n_iter *
#: n_matches) pass, and a backtest calls this once per match -- but many matches
#: in our data share an exact kickoff_at (whole CL rounds kick off simultaneously),
#: so memoizing on the cutoff timestamp avoids redundant refits within one run.
#: Process-lifetime only; never persisted, never shared across runs.
_fit_cache: dict[tuple[dt.datetime | None, float], dict[int, tuple[float, float]]] = {}


def fit_team_ratings(
    session: Session, before: dt.datetime | None = None, xi: float = 0.0, n_iter: int = 30
) -> dict[int, tuple[float, float]]:
    """
    Iterative (alternating) fit of per-team attack/defense multipliers a_i, d_i
    such that expected_goals(..., a_i, d_i, a_j, d_j) matches each team's
    observed xG for/against across all its matches simultaneously -- unlike
    shrunk_attack_defense, which rates a team only against the league average,
    this accounts for strength of opposition (beating a weak attack is worth
    less than beating a strong one). Not a true MLE, no convergence proof, but
    a standard fixed-point iteration for this kind of multiplicative model.

    `xi` is an optional recency decay (exp(-xi * days_before_cutoff)); 0 means
    every match counts equally regardless of age.

    Needs real connectivity to be meaningful: a team whose matches are only
    against opponents who *themselves* only ever played that one team (a
    "leaf" in the opponent graph) isn't actually identified -- the fit can
    equally well explain the data as "this team is strong" or "its few
    opponents are all weak", and which one it lands on depends on
    initialization/iteration order, not signal. Verified by hand against a
    hand-built fully-connected 3-team round robin (see
    tests/test_xg_model.py); do not trust this on a sparse/disconnected
    dataset -- in our own current one, 139 of 227 teams appear in exactly
    one match, i.e. are leaves, see README "Результаты бэктеста".
    """
    # id(session) scopes the cache to one Session/DB -- without it, two different
    # test databases (or two different DB connections in the same process) calling
    # fit_team_ratings(before=None) would collide on the same cache key and one
    # would silently get the other's fitted ratings back.
    cache_key = (id(session), before, xi)
    if cache_key in _fit_cache:
        return _fit_cache[cache_key]

    rows = _load_xg_rows(session, before)
    if not rows:
        _fit_cache[cache_key] = {}
        return {}

    teams = {r.home_id for r in rows} | {r.away_id for r in rows}
    a = dict.fromkeys(teams, 1.0)
    d = dict.fromkeys(teams, 1.0)

    mu = league_average_xg(session, before=before)
    home_adv = home_advantage_factor(session, before=before)
    base_home = mu * 2 * home_adv / (1 + home_adv)
    base_away = mu * 2 / (1 + home_adv)

    anchor = before or max(r.kickoff_at for r in rows)
    weights = [math.exp(-xi * max((anchor - r.kickoff_at).days, 0)) for r in rows]

    for _ in range(n_iter):
        num_a: dict[int, float] = dict.fromkeys(teams, 0.0)
        den_a: dict[int, float] = dict.fromkeys(teams, 0.0)
        for r, w in zip(rows, weights):
            num_a[r.home_id] += w * r.home_xg
            den_a[r.home_id] += w * base_home * d[r.away_id]
            num_a[r.away_id] += w * r.away_xg
            den_a[r.away_id] += w * base_away * d[r.home_id]
        a = {t: (num_a[t] / den_a[t] if den_a[t] > 0 else 1.0) for t in teams}

        num_d: dict[int, float] = dict.fromkeys(teams, 0.0)
        den_d: dict[int, float] = dict.fromkeys(teams, 0.0)
        for r, w in zip(rows, weights):
            num_d[r.home_id] += w * r.away_xg  # home team conceded the away team's xG
            den_d[r.home_id] += w * base_away * a[r.away_id]
            num_d[r.away_id] += w * r.home_xg
            den_d[r.away_id] += w * base_home * a[r.home_id]
        d = {t: (num_d[t] / den_d[t] if den_d[t] > 0 else 1.0) for t in teams}

        mean_a = sum(a.values()) / len(a)
        mean_d = sum(d.values()) / len(d)
        a = {t: v / mean_a for t, v in a.items()}
        d = {t: v / mean_d for t, v in d.items()}

    result = {t: (a[t], d[t]) for t in teams}
    _fit_cache[cache_key] = result
    return result


def expected_goals(
    league_avg: float, home_adv: float, attack_home: float, defense_home: float, attack_away: float, defense_away: float
) -> tuple[float, float]:
    """
    (lambda_home, lambda_away) for two average teams (attack=defense=1) satisfies
    (lambda_home + lambda_away) / 2 == league_avg and lambda_home / lambda_away ==
    home_adv -- splitting the home advantage between both sides this way, instead
    of multiplying only lambda_home by it (the earlier version of this function),
    avoided inflating the league's total expected goals whenever home_adv != 1.
    """
    base_home = league_avg * 2 * home_adv / (1 + home_adv)
    base_away = league_avg * 2 / (1 + home_adv)
    return base_home * attack_home * defense_away, base_away * attack_away * defense_home


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


def _prediction_from_lambdas(home_team_id: int, away_team_id: int, lambda_home: float, lambda_away: float) -> MatchPrediction:
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


def predict_match(
    session: Session, home_team_id: int, away_team_id: int, before: dt.datetime | None = None
) -> MatchPrediction | None:
    """
    `before` restricts all team-strength and league-average numbers to
    matches that kicked off strictly before that timestamp. Required for a
    fair backtest (predicting match X using data that includes match X, or
    matches after it, isn't a prediction); leave it unset for a genuinely
    upcoming match.

    Rates each team only against the league average (shrunk by sample size),
    not against the specific opponents it faced -- see predict_match_fitted
    for the version that accounts for strength of opposition.
    """
    home = team_xg_strength(session, home_team_id, before=before)
    away = team_xg_strength(session, away_team_id, before=before)
    if home is None or away is None:
        return None

    league_avg = league_average_xg(session, before=before)
    home_adv = home_advantage_factor(session, before=before)

    attack_home, defense_home = shrunk_attack_defense(home, league_avg)
    attack_away, defense_away = shrunk_attack_defense(away, league_avg)

    lambda_home, lambda_away = expected_goals(league_avg, home_adv, attack_home, defense_home, attack_away, defense_away)
    return _prediction_from_lambdas(home_team_id, away_team_id, lambda_home, lambda_away)


def predict_match_fitted(
    session: Session, home_team_id: int, away_team_id: int, before: dt.datetime | None = None, xi: float = 0.0
) -> MatchPrediction | None:
    """
    Same Poisson-grid machinery as predict_match, but attack/defense ratings
    come from fit_team_ratings' opponent-adjusted iterative fit instead of a
    team's average against the league as a whole -- beating a strong defense
    counts for more than beating a weak one. `xi` is the recency decay passed
    through to fit_team_ratings.
    """
    ratings = fit_team_ratings(session, before=before, xi=xi)
    if home_team_id not in ratings or away_team_id not in ratings:
        return None

    attack_home, defense_home = ratings[home_team_id]
    attack_away, defense_away = ratings[away_team_id]
    league_avg = league_average_xg(session, before=before)
    home_adv = home_advantage_factor(session, before=before)

    lambda_home, lambda_away = expected_goals(league_avg, home_adv, attack_home, defense_home, attack_away, defense_away)
    return _prediction_from_lambdas(home_team_id, away_team_id, lambda_home, lambda_away)


def devig_1x2(odds_home: float, odds_draw: float, odds_away: float) -> tuple[float, float, float]:
    """Decimal odds -> de-vigged implied probabilities (bookmaker margin removed by normalizing)."""
    raw = (1 / odds_home, 1 / odds_draw, 1 / odds_away)
    total = sum(raw)
    return (raw[0] / total, raw[1] / total, raw[2] / total)
