import datetime as dt
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from newstats.db.base import Base
from newstats.db.models import MatchStatus
from newstats.importer.flashscore import (
    extract_match_id,
    ingest_event,
    ingest_match_events,
    ingest_match_stats,
    ingest_odds,
    ingest_standings,
    normalize_status,
    parse_kickoff,
    parse_score,
)
from newstats.stats.form import form_summary
from newstats.stats.head_to_head import head_to_head
from newstats.stats.odds_movement import drift, movement

FIXTURES = Path(__file__).parent / "fixtures" / "flashscore"

MATCH_1_URL = "https://www.flashscore.com/match/football/team-a-AbC123/team-b-XyZ987/?mid=U7CL8Og5"
MATCH_2_URL = "https://www.flashscore.com/match/football/team-b-XyZ987/team-a-AbC123/?mid=ZZ999ZZ"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_extract_match_id_prefers_mid():
    assert extract_match_id(MATCH_1_URL) == "U7CL8Og5"


def test_extract_match_id_falls_back_to_path_without_mid():
    url = "https://www.flashscore.com/match/football/team-a-AbC123/team-b-XyZ987/odds/1x2-odds"
    assert extract_match_id(url) == "/match/football/team-a-AbC123/team-b-XyZ987"


def test_parse_score():
    assert parse_score("2 - 1") == (2, 1)
    assert parse_score("Awaiting updates") == (None, None)
    assert parse_score(None) == (None, None)


def test_normalize_status():
    assert normalize_status("Finished") == MatchStatus.FINISHED
    assert normalize_status("Postp.") == MatchStatus.POSTPONED
    assert normalize_status("something new from the site") == MatchStatus.UNKNOWN


def test_parse_kickoff():
    assert parse_kickoff("13.10.2026 22:00") == dt.datetime(2026, 10, 13, 22, 0)
    assert parse_kickoff(None) is None
    assert parse_kickoff("not a date") is None


def test_ingest_event_sets_kickoff_at():
    session = _session()
    match = ingest_event(session, MATCH_1_URL, _load("event.json"))
    assert match.kickoff_at == dt.datetime(2026, 5, 5, 18, 0)


def test_ingest_match_stats_requires_existing_match():
    session = _session()
    with pytest.raises(ValueError):
        ingest_match_stats(session, MATCH_1_URL, [{"label": "Corner kicks", "home": "2", "away": "5"}])


def test_ingest_match_stats_parses_percent_and_bracketed_values():
    session = _session()
    ingest_event(session, MATCH_1_URL, _load("event.json"))
    rows = ingest_match_stats(
        session,
        MATCH_1_URL,
        [
            {"label": "Corner kicks", "home": "2", "away": "5"},
            {"label": "Ball possession", "home": "35%", "away": "65%"},
            {"label": "Accurate passes", "home": "285 (81%)", "away": "(93%) 589"},
        ],
    )
    assert len(rows) == 6
    by_name = {(r.team_id, r.stat_name): r.value for r in rows}
    from newstats.db.models import Match

    m = session.query(Match).one()
    assert by_name[(m.home_team_id, "corners")] == 2.0
    assert by_name[(m.away_team_id, "corners")] == 5.0
    assert by_name[(m.home_team_id, "possession_pct")] == 35.0
    assert by_name[(m.home_team_id, "accurate_passes")] == 285.0
    assert by_name[(m.away_team_id, "accurate_passes")] == 93.0


def test_ingest_match_events_requires_existing_match():
    session = _session()
    with pytest.raises(ValueError):
        ingest_match_events(session, MATCH_1_URL, [])


def test_ingest_match_events_aggregates_goals_and_cards():
    session = _session()
    ingest_event(session, MATCH_1_URL, _load("event.json"))
    incidents = [
        {"type": "goal", "side": "home", "player_name": "Barrios P.", "player_link": "/player/barrios-pablo/AaZiGJ9E/"},
        {"type": "assist", "side": "home", "player_name": "Baena A.", "player_link": "/player/baena-alex/bmeQTB92/"},
        {"type": "goal", "side": "home", "player_name": "Barrios P.", "player_link": "/player/barrios-pablo/AaZiGJ9E/"},
        {"type": "yellow_card", "side": "away", "player_name": "Rashford M.", "player_link": "/player/rashford-marcus/COnT2Lh0/"},
        {"type": "substitution", "side": "home", "player_name": "Someone", "player_link": "/player/x/y/"},
    ]
    rows = ingest_match_events(session, MATCH_1_URL, incidents)
    assert len(rows) == 3  # 2 goals (deduped/aggregated to one row, value=2) + 1 assist + 1 yellow = 3 rows total

    from newstats.db.models import Player

    barrios = session.query(Player).filter_by(external_id="/player/barrios-pablo/AaZiGJ9E/").one()
    goal_row = next(r for r in rows if r.player_id == barrios.id and r.stat_name == "goals")
    assert goal_row.value == 2.0

    # re-running with the same incidents must not double-count (idempotent upsert)
    rows_again = ingest_match_events(session, MATCH_1_URL, incidents)
    goal_row_again = next(r for r in rows_again if r.player_id == barrios.id and r.stat_name == "goals")
    assert goal_row_again.value == 2.0


def test_ingest_event_is_idempotent():
    session = _session()
    first = ingest_event(session, MATCH_1_URL, _load("event.json"))
    second = ingest_event(session, MATCH_1_URL, _load("event.json"))
    assert first.id == second.id
    assert first.home_score == 2
    assert first.away_score == 1
    assert first.status == MatchStatus.FINISHED
    assert first.status_raw == "Finished"


def test_ingest_odds_requires_existing_match():
    session = _session()
    try:
        ingest_odds(session, MATCH_1_URL, _load("odds_1x2.json"), dt.datetime.now(dt.UTC))
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_ingest_odds_ignores_odds_results_own_mid_less_url():
    """Real scraper output: odds_result["url"] strips ?mid= -- must not be used to find the match."""
    session = _session()
    ingest_event(session, MATCH_1_URL, _load("event.json"))
    odds = _load("odds_1x2.json")
    odds["url"] = "https://www.flashscore.com/match/football/team-a-AbC123/team-b-XyZ987/odds/1x2-odds"  # no ?mid=
    rows = ingest_odds(session, MATCH_1_URL, odds, dt.datetime.now(dt.UTC))
    assert len(rows) == 3


def test_ingest_odds_and_movement():
    session = _session()
    ingest_event(session, MATCH_1_URL, _load("event.json"))
    t1 = dt.datetime(2026, 5, 5, 10, 0, tzinfo=dt.UTC)
    ingest_odds(session, MATCH_1_URL, _load("odds_1x2.json"), t1)

    odds_later = _load("odds_1x2.json")
    odds_later["data"][0]["odds_values"][0]["odds"] = "1.75"
    t2 = dt.datetime(2026, 5, 5, 17, 0, tzinfo=dt.UTC)
    ingest_odds(session, MATCH_1_URL, odds_later, t2)

    from newstats.db.models import Match

    match = session.query(Match).one()
    points = movement(session, match.id, "1x2-odds", "Home")
    assert [p["value"] for p in points] == [1.85, 1.75]
    assert drift(session, match.id, "1x2-odds", "Home") == pytest.approx(-0.10)


def test_ingest_standings_requires_existing_match():
    session = _session()
    try:
        ingest_standings(session, MATCH_1_URL, _load("standings.json"), dt.datetime.now(dt.UTC))
        raised = False
    except ValueError:
        raised = True
    assert raised


def test_ingest_standings_after_event():
    session = _session()
    ingest_event(session, MATCH_1_URL, _load("event.json"))
    rows = ingest_standings(session, MATCH_1_URL, _load("standings.json"), dt.datetime.now(dt.UTC))
    assert len(rows) == 2
    assert {r.rank for r in rows} == {1, 2}


def test_form_and_h2h_across_two_matches():
    session = _session()
    team_a = ingest_event(session, MATCH_1_URL, _load("event.json")).home_team_id
    team_b = ingest_event(session, MATCH_1_URL, _load("event.json")).away_team_id
    ingest_event(session, MATCH_2_URL, _load("event_return.json"))

    summary = form_summary(session, team_a)
    assert summary["matches_considered"] == 2
    assert summary["wins"] == 2
    assert summary["goals_for"] == 5
    assert summary["goals_against"] == 1

    h2h = head_to_head(session, team_a, team_b)
    assert h2h["matches_considered"] == 2
    assert h2h["team_a_wins"] == 2
