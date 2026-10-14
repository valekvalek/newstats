import datetime as dt

from conftest import make_session as _session
from simulate_bets import SELECTIONS, decide_bet

from newstats.importer.flashscore import ingest_event, ingest_odds
from newstats.stats.value_bets import snapshot_recorded_at


def test_decide_bet_picks_highest_ev_above_threshold():
    probs = {"Home": 0.6, "Draw": 0.25, "Away": 0.15}
    odds = {"Home": 2.0, "Draw": 3.4, "Away": 5.0}  # EV: 0.2, -0.15, -0.25
    assert decide_bet(probs, odds, ev_min=0.05) == ("Home", 0.6 * 2.0 - 1)


def test_decide_bet_returns_none_when_nothing_clears_the_threshold():
    probs = {"Home": 0.4, "Draw": 0.3, "Away": 0.3}
    odds = {"Home": 2.5, "Draw": 3.33, "Away": 3.33}  # all ~break-even
    assert decide_bet(probs, odds, ev_min=0.05) is None


def test_decide_bet_at_most_one_selection():
    probs = {"Home": 0.33, "Draw": 0.33, "Away": 0.34}
    odds = {s: 3.0 for s in SELECTIONS}  # identical EV for all three
    decision = decide_bet(probs, odds, ev_min=0.0)
    assert decision is not None
    assert decision[0] in SELECTIONS


def _event(home: str, away: str) -> dict:
    return {
        "date": "01.01.2026 20:00",
        "participants": [
            {"role": "home", "name": home, "img": None, "link": f"/team/{home.lower()}/{home}1/"},
            {"role": "away", "name": away, "img": None, "link": f"/team/{away.lower()}/{away}1/"},
        ],
        "score": "1-0",
        "detail_status": "Finished",
        "league": {"name": "Test League", "link": "/test/league/"},
    }


def _odds_payload(home: str, draw: str, away: str) -> dict:
    return {
        "url": "https://fs.test/m1/odds/1x2-odds?mid=m1",
        "odd_type": "1x2-odds",
        "data": [
            {
                "bookmaker_id": "16",
                "bookmaker_name": "bet365",
                "bookmaker_link": "/b/16",
                "odds_values": [
                    {"value": "1", "type": "Home", "odds": home},
                    {"value": "X", "type": "Draw", "odds": draw},
                    {"value": "2", "type": "Away", "odds": away},
                ],
            }
        ],
        "error": None,
    }


def test_snapshot_recorded_at_picks_the_right_snapshot_and_bookmaker():
    session = _session()
    ingest_event(session, "https://fs.test/m1?mid=m1", _event("A", "B"))

    t1 = dt.datetime(2026, 1, 1, 10, 0)
    t2 = dt.datetime(2026, 1, 2, 9, 0)
    ingest_odds(session, "https://fs.test/m1?mid=m1", _odds_payload("2.00", "3.40", "3.20"), t1, snapshot="t-1h")
    ingest_odds(session, "https://fs.test/m1?mid=m1", _odds_payload("1.90", "3.50", "3.80"), t2, snapshot="close")

    match_id = ingest_event(session, "https://fs.test/m1?mid=m1", _event("A", "B")).id
    assert snapshot_recorded_at(session, match_id, "bet365", "t-1h") == t1
    assert snapshot_recorded_at(session, match_id, "bet365", "close") == t2
    assert snapshot_recorded_at(session, match_id, "bet365", "t-24h") is None  # never recorded
    assert snapshot_recorded_at(session, match_id, "pinnacle", "t-1h") is None  # wrong bookmaker
