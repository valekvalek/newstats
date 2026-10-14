import math

from backtest import load_matches
from conftest import make_session as _session

from newstats.importer.flashscore import ingest_event
from newstats.stats.metrics import actual_outcome, bootstrap_ci, brier3, logloss3


def _event(date: str, home: str, away: str, score: str, status: str) -> dict:
    return {
        "date": date,
        "participants": [
            {"role": "home", "name": home, "img": None, "link": f"/team/{home.lower()}/{home}1/"},
            {"role": "away", "name": away, "img": None, "link": f"/team/{away.lower()}/{away}1/"},
        ],
        "score": score,
        "detail_status": status,
        "league": {"name": "Test League", "link": "/test/league/"},
    }


def test_actual_outcome():
    assert actual_outcome(2, 1) == "home"
    assert actual_outcome(1, 2) == "away"
    assert actual_outcome(1, 1) == "draw"


def test_brier3_perfect_prediction_is_zero():
    assert brier3((1, 0, 0), 0) == 0


def test_brier3_penalizes_wrong_confident_prediction():
    assert brier3((1, 0, 0), 2) == 2  # (1-0)^2 + (0-0)^2 + (0-1)^2 = 1 + 1 = 2


def test_logloss3_uniform_prediction_is_ln3():
    assert math.isclose(logloss3((1 / 3, 1 / 3, 1 / 3), 0), math.log(3), rel_tol=1e-9)


def test_logloss3_perfect_prediction_is_near_zero():
    assert logloss3((1.0, 0.0, 0.0), 0) < 1e-6


def test_bootstrap_ci_brackets_the_true_mean_for_constant_data():
    lo, hi = bootstrap_ci([5.0] * 50)
    assert lo == hi == 5.0


def test_bootstrap_ci_is_narrower_for_larger_samples():
    rng_data_small = [0.0, 1.0] * 5  # n=10, high variance in the mean estimate
    rng_data_large = [0.0, 1.0] * 500  # n=1000
    lo_s, hi_s = bootstrap_ci(rng_data_small, seed=1)
    lo_l, hi_l = bootstrap_ci(rng_data_large, seed=1)
    assert (hi_l - lo_l) < (hi_s - lo_s)


def test_load_matches_only_plain_90_minute_results():
    """status_raw == "Finished" only -- NOT MatchStatus.FINISHED. Both "After
    Extra Time" (score is the 120-minute result, not what the 1X2 market
    settles on) and "After 90 minutes" (penalty shootout) must be excluded,
    even though both are MatchStatus.FINISHED in the DB."""
    session = _session()
    ingest_event(session, "https://fs.test/finished?mid=finished", _event("13.10.2026 20:00", "A", "B", "2-1", "Finished"))
    ingest_event(session, "https://fs.test/scheduled?mid=scheduled", _event("20.10.2026 20:00", "C", "D", "-", ""))
    ingest_event(session, "https://fs.test/pens?mid=pens", _event("13.10.2026 20:00", "E", "F", "1-1", "After 90 minutes"))
    ingest_event(session, "https://fs.test/et?mid=et", _event("13.10.2026 20:00", "G", "H", "3-1", "After Extra Time"))

    matches = load_matches(session)
    assert {m.status_raw for m in matches} == {"Finished"}
    assert len(matches) == 1


def test_load_matches_since_until_range():
    session = _session()
    ingest_event(session, "https://fs.test/early?mid=early", _event("01.09.2026 20:00", "A", "B", "1-0", "Finished"))
    ingest_event(session, "https://fs.test/mid?mid=mid", _event("15.09.2026 20:00", "C", "D", "1-0", "Finished"))
    ingest_event(session, "https://fs.test/late?mid=late", _event("01.10.2026 20:00", "E", "F", "1-0", "Finished"))

    matches = load_matches(session, since="2026-09-10", until="2026-09-20")
    assert len(matches) == 1
    assert matches[0].home_team.name == "C"
