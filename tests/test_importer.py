from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from newstats.db.base import Base
from newstats.importer.loader import load_directory
from newstats.stats.form import form_summary
from newstats.stats.head_to_head import head_to_head

FIXTURES = Path(__file__).parent / "fixtures" / "scraper_sample"


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_import_is_idempotent():
    session = _session()
    first = load_directory(session, FIXTURES)
    second = load_directory(session, FIXTURES)
    assert first == second == 2


def test_form_summary_reflects_imported_matches():
    session = _session()
    load_directory(session, FIXTURES)
    summary = form_summary(session, team_id=1)
    assert summary["matches_considered"] == 2
    assert summary["wins"] == 2
    assert summary["goals_for"] == 5
    assert summary["goals_against"] == 1


def test_head_to_head_between_two_teams():
    session = _session()
    load_directory(session, FIXTURES)
    result = head_to_head(session, team_a_id=1, team_b_id=2)
    assert result["matches_considered"] == 2
    assert result["team_a_wins"] == 2
    assert result["team_b_wins"] == 0
