import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from newstats.db.base import Base

#: scripts/ isn't a package (no __init__.py, and it never should be -- these are
#: standalone CLI entry points, not library code) but a couple of them have pure
#: functions worth unit-testing directly (e.g. backtest.py's load_matches,
#: simulate_bets.py's decide_bet). Add it to sys.path for test collection only.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))


def make_session() -> Session:
    """
    Shared test-DB helper: SQLite in-memory by default (fast, zero setup), or a
    real TEST_DATABASE_URL (e.g. Postgres in CI, see .github/workflows/ci.yml)
    when set -- so the ORM is exercised against the engine that actually runs
    in production too, not just SQLite's more forgiving dialect. This still
    uses create_all, not `alembic upgrade head` -- it does NOT verify the
    migration itself is correct; CI runs that separately as its own step
    against the same Postgres service. Drops and recreates every table first,
    since a persistent DB (unlike a fresh in-memory SQLite) can have leftover
    rows from a previous test.
    """
    url = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


@pytest.fixture(autouse=True)
def _clear_xg_fit_cache():
    """
    fit_team_ratings() caches by id(session) (see its module for why) --
    across many short-lived Session objects in a test run, a freed session's
    id can be reused by a later one, which would silently return a stale
    fit computed against a different, already-garbage-collected database.
    Not a risk in a real script's single long-lived session, only here.
    """
    from newstats.stats import xg_model

    xg_model._fit_cache.clear()
    yield
    xg_model._fit_cache.clear()
