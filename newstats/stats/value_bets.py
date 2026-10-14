"""Compare the xG Poisson model's 1X2 probabilities against the latest market odds."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from newstats.db.models import Odds
from newstats.stats.xg_model import MatchPrediction, devig_1x2, predict_match


@dataclass
class OddsComparison:
    bookmaker: str
    odds_home: float
    odds_draw: float
    odds_away: float
    market_p_home: float
    market_p_draw: float
    market_p_away: float
    edge_home: float  # model probability minus de-vigged market probability
    edge_draw: float
    edge_away: float


#: Flashscore labels the 1X2 selections "Home"/"Draw"/"Away" in some locales and
#: plain "1"/"X"/"2" in others (confirmed live: a DE-locale fetch returned "1"/"X"/"2"
#: for the same `odd_type="1x2-odds"` market) -- normalize both to one internal key.
_SELECTION_ALIASES = {"1": "Home", "X": "Draw", "2": "Away", "Home": "Home", "Draw": "Draw", "Away": "Away"}


def latest_1x2_odds(session: Session, match_id: int, snapshot: str | None = None) -> dict[str, dict[str, float]]:
    """
    {bookmaker_name: {"Home": odds, "Draw": odds, "Away": odds}}, latest quote
    per (bookmaker, selection). Pass snapshot="close" to get the closing-line
    odds specifically (requires scripts/snapshot_odds.py to have run);
    without it, this is just whatever was last ingested, with no guarantee
    of when relative to kickoff -- fine for a rough look, not for CLV.
    """
    query = select(Odds).where(Odds.match_id == match_id, Odds.market == "1x2-odds")
    if snapshot is not None:
        query = query.where(Odds.snapshot == snapshot)
    rows = session.execute(query.order_by(Odds.recorded_at.desc())).scalars()

    latest: dict[str, dict[str, float]] = {}
    seen: set[tuple[str, str]] = set()
    for row in rows:
        selection = _SELECTION_ALIASES.get(row.selection)
        if selection is None:
            continue
        key = (row.bookmaker, selection)
        if key in seen:
            continue
        seen.add(key)
        latest.setdefault(row.bookmaker, {})[selection] = float(row.value)
    return {bm: sels for bm, sels in latest.items() if {"Home", "Draw", "Away"} <= sels.keys()}


def compare_to_market(session: Session, match_id: int, home_team_id: int, away_team_id: int) -> tuple[MatchPrediction | None, list[OddsComparison]]:
    prediction = predict_match(session, home_team_id, away_team_id)
    books = latest_1x2_odds(session, match_id)

    comparisons = []
    for bookmaker, odds in books.items():
        p_home, p_draw, p_away = devig_1x2(odds["Home"], odds["Draw"], odds["Away"])
        comparisons.append(
            OddsComparison(
                bookmaker=bookmaker,
                odds_home=odds["Home"],
                odds_draw=odds["Draw"],
                odds_away=odds["Away"],
                market_p_home=p_home,
                market_p_draw=p_draw,
                market_p_away=p_away,
                edge_home=(prediction.p_home_win - p_home) if prediction else float("nan"),
                edge_draw=(prediction.p_draw - p_draw) if prediction else float("nan"),
                edge_away=(prediction.p_away_win - p_away) if prediction else float("nan"),
            )
        )
    return prediction, comparisons


def clv(odds_taken: float, p_close: float) -> float:
    """
    Closing Line Value for a bet taken at `odds_taken` on an outcome whose
    de-vigged closing probability turned out to be `p_close`. Positive means
    you beat the closing price (the standard proxy for "this was a good bet
    on process", independent of whether it actually won) -- see
    scripts/simulate_bets.py and snapshot="close" in scripts/snapshot_odds.py.
    """
    return odds_taken * p_close - 1
