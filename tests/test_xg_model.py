import datetime as dt

import pytest
from conftest import make_session as _session

from newstats.db.models import Team
from newstats.importer.flashscore import ingest_event, ingest_match_stats, ingest_odds
from newstats.stats.value_bets import clv, compare_to_market, latest_1x2_odds
from newstats.stats.xg_model import (
    TeamXgStrength,
    devig_1x2,
    expected_goals,
    fit_team_ratings,
    predict_match,
    predict_match_fitted,
    shrunk_attack_defense,
    team_xg_strength,
)


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


def _event_at(date_str: str, home_name: str, home_link: str, away_name: str, away_link: str) -> dict:
    e = _event(home_name, home_link, away_name, away_link)
    e["date"] = date_str
    return e


def test_team_xg_strength_before_excludes_matches_on_or_after_cutoff():
    """A backtest must not let a team's strength be computed from the match being
    predicted, or from matches that happened after it -- that's lookahead bias."""
    session = _session()
    early = ingest_event(session, "https://fs.test/early?mid=early", _event_at("01.01.2026 20:00", "A", "/team/a/A1/", "X", "/team/x/X1/"))
    _ingest_xg(session, "https://fs.test/early?mid=early", 1.0, 1.0)
    late = ingest_event(session, "https://fs.test/late?mid=late", _event_at("10.01.2026 20:00", "A", "/team/a/A1/", "Y", "/team/y/Y1/"))
    _ingest_xg(session, "https://fs.test/late?mid=late", 5.0, 0.2)

    cutoff = dt.datetime(2026, 1, 5)
    strength = team_xg_strength(session, early.home_team_id, before=cutoff)
    assert strength.matches_considered == 1
    assert strength.xg_for_avg == 1.0  # only the early match counted, not the later 5.0 one

    strength_no_cutoff = team_xg_strength(session, early.home_team_id)
    assert strength_no_cutoff.matches_considered == 2
    assert late.home_team_id == early.home_team_id  # sanity: same team in both matches


def test_shrunk_attack_defense_pulls_small_samples_toward_league_average():
    league_avg = 1.4
    one_match = TeamXgStrength(team_id=1, xg_for_avg=3.0, xg_against_avg=0.2, matches_considered=1)
    ten_matches = TeamXgStrength(team_id=2, xg_for_avg=3.0, xg_against_avg=0.2, matches_considered=10)

    attack_1, defense_1 = shrunk_attack_defense(one_match, league_avg)
    attack_10, defense_10 = shrunk_attack_defense(ten_matches, league_avg)

    # both ratings pull toward 1.0 (league average), but the 1-match sample pulls harder
    assert 1.0 < attack_1 < attack_10
    assert defense_10 < defense_1 < 1.0


def test_shrunk_attack_defense_exact_at_k_matches_is_50_50():
    league_avg = 2.0
    strength = TeamXgStrength(team_id=1, xg_for_avg=4.0, xg_against_avg=1.0, matches_considered=5)  # n == K == 5.0
    attack, defense = shrunk_attack_defense(strength, league_avg)
    assert attack == pytest.approx((0.5 * 4.0 + 0.5 * 2.0) / 2.0)
    assert defense == pytest.approx((0.5 * 1.0 + 0.5 * 2.0) / 2.0)


def test_expected_goals_average_teams_match_league_avg_and_home_adv():
    league_avg, home_adv = 1.4, 1.2
    lh, la = expected_goals(league_avg, home_adv, attack_home=1.0, defense_home=1.0, attack_away=1.0, defense_away=1.0)
    assert (lh + la) / 2 == pytest.approx(league_avg)
    assert lh / la == pytest.approx(home_adv)


def test_clv_positive_when_price_beat_the_close():
    # Took 2.50, closing de-vigged probability implies a "fair" price of ~2.22 (1/0.45) -- so we beat it.
    assert clv(2.50, 0.45) > 0
    # Took 1.80 on something that closed implying ~2.22 fair -- we didn't beat it.
    assert clv(1.80, 0.45) < 0


def test_latest_1x2_odds_filters_by_snapshot():
    session = _session()
    match = ingest_event(session, "https://fs.test/m1?mid=m1", _event("A", "/team/a/A1/", "B", "/team/b/B1/"))
    early = {
        "url": "https://fs.test/m1/odds/1x2-odds?mid=m1",
        "odd_type": "1x2-odds",
        "data": [{"bookmaker_id": "2", "bookmaker_name": "Bet365", "bookmaker_link": "/b/2", "odds_values": [{"value": "1", "type": "Home", "odds": "2.10"}, {"value": "X", "type": "Draw", "odds": "3.40"}, {"value": "2", "type": "Away", "odds": "3.20"}]}],
        "error": None,
    }
    close = {
        "url": "https://fs.test/m1/odds/1x2-odds?mid=m1",
        "odd_type": "1x2-odds",
        "data": [{"bookmaker_id": "2", "bookmaker_name": "Bet365", "bookmaker_link": "/b/2", "odds_values": [{"value": "1", "type": "Home", "odds": "1.90"}, {"value": "X", "type": "Draw", "odds": "3.50"}, {"value": "2", "type": "Away", "odds": "3.80"}]}],
        "error": None,
    }
    ingest_odds(session, "https://fs.test/m1?mid=m1", early, dt.datetime(2026, 1, 1), snapshot="t-24h")
    ingest_odds(session, "https://fs.test/m1?mid=m1", close, dt.datetime(2026, 1, 2), snapshot="close")

    closing = latest_1x2_odds(session, match.id, snapshot="close")
    assert closing == {"Bet365": {"Home": 1.9, "Draw": 3.5, "Away": 3.8}}

    opening = latest_1x2_odds(session, match.id, snapshot="t-24h")
    assert opening == {"Bet365": {"Home": 2.1, "Draw": 3.4, "Away": 3.2}}

    everything = latest_1x2_odds(session, match.id)
    assert everything == closing  # no snapshot filter -> most recently recorded, which is the close


def test_fit_team_ratings_separates_strong_mid_weak_in_a_connected_league():
    """A fully-connected round robin (every team plays every other team, so no
    team's rating is determined by only one opponent) should recover the
    obvious ordering: Strong scores a lot and concedes little, Weak the
    opposite, Mid in between -- on both the attack and defense axes."""
    session = _session()
    counter = [0]

    def play(home, away, home_xg, away_xg):
        counter[0] += 1
        url = f"https://fs.test/rr{counter[0]}?mid=rr{counter[0]}"
        ingest_event(session, url, _event(home[0], home[1], away[0], away[1]))
        _ingest_xg(session, url, home_xg, away_xg)

    strong, mid, weak = ("Strong", "/team/strong/S1/"), ("Mid", "/team/mid/M1/"), ("Weak", "/team/weak/W1/")
    play(strong, mid, 3.0, 0.5)
    play(mid, strong, 0.5, 3.0)
    play(strong, weak, 3.5, 0.3)
    play(weak, strong, 0.3, 3.5)
    play(mid, weak, 1.5, 1.0)
    play(weak, mid, 1.0, 1.5)

    ratings = fit_team_ratings(session)
    teams = {name: session.query(Team).filter_by(name=name).one().id for name in ("Strong", "Mid", "Weak")}
    attack_s, defense_s = ratings[teams["Strong"]]
    attack_m, defense_m = ratings[teams["Mid"]]
    attack_w, defense_w = ratings[teams["Weak"]]

    assert attack_s > attack_m > attack_w  # higher = better attack
    assert defense_s < defense_m < defense_w  # lower = better defense
    assert attack_s > 1.0 > attack_w
    assert defense_s < 1.0 < defense_w


def test_fit_team_ratings_empty_without_data():
    session = _session()
    assert fit_team_ratings(session) == {}


def test_predict_match_fitted_returns_none_for_unknown_team():
    session = _session()
    ingest_event(session, "https://fs.test/m1?mid=m1", _event("A", "/team/a/A1/", "B", "/team/b/B1/"))
    _ingest_xg(session, "https://fs.test/m1?mid=m1", 1.5, 1.0)
    assert predict_match_fitted(session, home_team_id=999, away_team_id=998) is None
