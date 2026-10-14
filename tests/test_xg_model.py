from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from newstats.db.base import Base
from newstats.importer.flashscore import ingest_event, ingest_match_stats, ingest_odds
from newstats.stats.value_bets import compare_to_market
from newstats.stats.xg_model import devig_1x2, predict_match, team_xg_strength


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _event(home_name: str, home_link: str, away_name: str, away_link: str) -> dict:
    return {
        "date": "01.01.2026 20:00",
        "participants": [
            {"role": "home", "name": home_name, "img": None, "link": home_link},
            {"role": "away", "name": away_name, "img": None, "link": away_link},
        ],
        "score": "1 - 0",
        "detail_status": "Finished",
        "league": {"name": "Test League", "link": "/football/test/league/"},
    }


def _ingest_xg(session, url, home_xg, away_xg):
    ingest_match_stats(session, url, [{"label": "Expected goals (xG)", "home": str(home_xg), "away": str(away_xg)}])


def test_devig_1x2_removes_margin_and_preserves_ratio():
    p_home, p_draw, p_away = devig_1x2(1.85, 3.40, 4.20)
    assert abs((p_home + p_draw + p_away) - 1.0) < 1e-9
    # raw implied probs are proportional to 1/odds -- devig just rescales them
    assert p_home > p_draw > p_away


def test_team_xg_strength_none_when_no_data():
    session = _session()
    assert team_xg_strength(session, team_id=999) is None


def test_predict_match_favors_the_stronger_attacking_team():
    session = _session()

    # Team A: strong attack, weak concession, across 3 matches (home and away).
    m1 = ingest_event(session, "https://fs.test/m1?mid=m1", _event("A", "/team/a/A1/", "X1", "/team/x1/X1/"))
    _ingest_xg(session, "https://fs.test/m1?mid=m1", 2.6, 0.4)
    ingest_event(session, "https://fs.test/m2?mid=m2", _event("X2", "/team/x2/X2/", "A", "/team/a/A1/"))
    _ingest_xg(session, "https://fs.test/m2?mid=m2", 0.3, 2.2)
    ingest_event(session, "https://fs.test/m3?mid=m3", _event("A", "/team/a/A1/", "X3", "/team/x3/X3/"))
    _ingest_xg(session, "https://fs.test/m3?mid=m3", 2.9, 0.5)

    # Team B: weak attack, leaky defense, across 3 matches.
    m4 = ingest_event(session, "https://fs.test/m4?mid=m4", _event("B", "/team/b/B1/", "Y1", "/team/y1/Y1/"))
    _ingest_xg(session, "https://fs.test/m4?mid=m4", 0.4, 2.3)
    ingest_event(session, "https://fs.test/m5?mid=m5", _event("Y2", "/team/y2/Y2/", "B", "/team/b/B1/"))
    _ingest_xg(session, "https://fs.test/m5?mid=m5", 2.5, 0.3)
    ingest_event(session, "https://fs.test/m6?mid=m6", _event("B", "/team/b/B1/", "Y3", "/team/y3/Y3/"))
    _ingest_xg(session, "https://fs.test/m6?mid=m6", 0.5, 2.1)

    strength_a = team_xg_strength(session, m1.home_team_id)
    assert strength_a.matches_considered == 3
    assert strength_a.xg_for_avg > 2.0
    assert strength_a.xg_against_avg < 1.0

    prediction = predict_match(session, home_team_id=m1.home_team_id, away_team_id=m4.home_team_id)
    assert prediction is not None
    assert prediction.p_home_win > prediction.p_away_win
    total = prediction.p_home_win + prediction.p_draw + prediction.p_away_win
    assert abs(total - 1.0) < 1e-3


def test_compare_to_market_computes_edge_against_devigged_odds():
    session = _session()
    match = ingest_event(session, "https://fs.test/m1?mid=m1", _event("A", "/team/a/A1/", "B", "/team/b/B1/"))
    _ingest_xg(session, "https://fs.test/m1?mid=m1", 2.5, 0.6)

    # Give both teams at least one more match so the averages aren't single-sample.
    ingest_event(session, "https://fs.test/m2?mid=m2", _event("A", "/team/a/A1/", "C", "/team/c/C1/"))
    _ingest_xg(session, "https://fs.test/m2?mid=m2", 2.3, 0.5)
    ingest_event(session, "https://fs.test/m3?mid=m3", _event("D", "/team/d/D1/", "B", "/team/b/B1/"))
    _ingest_xg(session, "https://fs.test/m3?mid=m3", 2.0, 0.4)
    # balancing matches so home_advantage_factor isn't skewed purely by A/B's own fixtures
    ingest_event(session, "https://fs.test/m4?mid=m4", _event("E", "/team/e/E1/", "F", "/team/f/F1/"))
    _ingest_xg(session, "https://fs.test/m4?mid=m4", 0.7, 1.8)
    ingest_event(session, "https://fs.test/m5?mid=m5", _event("G", "/team/g/G1/", "H", "/team/h/H1/"))
    _ingest_xg(session, "https://fs.test/m5?mid=m5", 0.6, 2.0)

    odds_payload = {
        "url": "https://fs.test/m1/odds/1x2-odds?mid=m1",
        "odd_type": "1x2-odds",
        "data": [
            {
                "bookmaker_id": "2",
                "bookmaker_name": "Bet365",
                "bookmaker_link": "/bookmaker/2",
                "odds_values": [
                    {"value": "1", "type": "Home", "odds": "3.50"},  # market underrates the clearly stronger home team
                    {"value": "X", "type": "Draw", "odds": "3.40"},
                    {"value": "2", "type": "Away", "odds": "2.10"},
                ],
            }
        ],
        "error": None,
    }
    ingest_odds(session, "https://fs.test/m1?mid=m1", odds_payload, __import__("datetime").datetime(2026, 1, 1))

    prediction, comparisons = compare_to_market(session, match.id, match.home_team_id, match.away_team_id)
    assert prediction is not None
    assert len(comparisons) == 1
    assert comparisons[0].edge_home > 0  # model is more bullish on the home win than the market price implies


def test_latest_1x2_odds_accepts_numeric_selection_labels():
    """Some locales label selections '1'/'X'/'2' instead of 'Home'/'Draw'/'Away' -- both must work."""
    session = _session()
    match = ingest_event(session, "https://fs.test/m1?mid=m1", _event("A", "/team/a/A1/", "B", "/team/b/B1/"))
    odds_payload = {
        "url": "https://fs.test/m1/odds/1x2-odds?mid=m1",
        "odd_type": "1x2-odds",
        "data": [
            {
                "bookmaker_id": "16",
                "bookmaker_name": "bet365",
                "bookmaker_link": "/bookmaker/16",
                "odds_values": [
                    {"value": "1", "type": "1", "odds": "2.00"},
                    {"value": "X", "type": "X", "odds": "4.00"},
                    {"value": "2", "type": "2", "odds": "3.30"},
                ],
            }
        ],
        "error": None,
    }
    ingest_odds(session, "https://fs.test/m1?mid=m1", odds_payload, __import__("datetime").datetime(2026, 1, 1))

    from newstats.stats.value_bets import latest_1x2_odds

    books = latest_1x2_odds(session, match.id)
    assert books == {"bet365": {"Home": 2.0, "Draw": 4.0, "Away": 3.3}}
