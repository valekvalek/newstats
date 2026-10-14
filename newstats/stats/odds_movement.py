"""Line movement for a match/market — how the odds shifted, not just where they ended up."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from newstats.db.models import Odds


def movement(session: Session, match_id: int, market: str, selection: str) -> list[dict]:
    stmt = (
        select(Odds)
        .where(Odds.match_id == match_id, Odds.market == market, Odds.selection == selection)
        .order_by(Odds.recorded_at.asc())
    )
    rows = list(session.execute(stmt).scalars())
    return [
        {"bookmaker": r.bookmaker, "value": float(r.value), "recorded_at": r.recorded_at}
        for r in rows
    ]


def drift(session: Session, match_id: int, market: str, selection: str) -> float | None:
    """Difference between the latest and earliest recorded price, same bookmaker set."""
    points = movement(session, match_id, market, selection)
    if len(points) < 2:
        return None
    return points[-1]["value"] - points[0]["value"]
