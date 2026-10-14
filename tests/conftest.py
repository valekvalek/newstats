import os
import sys
from pathlib import Path

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
    when set -- so the ORM/migrations are exercised against the engine that
    actually runs in production too, not just SQLite's more forgiving dialect.
    Drops and recreates every table first, since a persistent DB (unlike a
    fresh in-memory SQLite) can have leftover rows from a previous test.
    """
    url = os.environ.get("TEST_DATABASE_URL", "sqlite:///:memory:")
    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()
