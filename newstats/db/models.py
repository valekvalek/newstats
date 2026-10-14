"""
Core schema: Sport -> Tournament -> Team -> Player, and Match -> stats/odds.

Every entity that originates from the external scraper carries (source,
external_id) so re-imports are idempotent (upsert on that pair) instead of
duplicating rows across scraper runs.

Stats are modeled as narrow key/value rows (MatchStat, PlayerMatchStat)
rather than fixed columns, because different sports expose different stat
sets (football corners/possession vs volleyball sets/aces) and the scraper's
own stat vocabulary will keep growing. A fixed-column table would need a
migration every time a new stat type shows up; this does not.
"""

from __future__ import annotations

import datetime as dt
import enum

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from newstats.db.base import Base


class MatchStatus(str, enum.Enum):
    """
    Normalized bucket. The scraper's own `detail_status` is free text
    ("Finished", "After Pen.", "Postp.", ...) and keeps changing wording per
    sport/locale, so it is kept verbatim in `Match.status_raw` and mapped
    into this small fixed set via `newstats.importer.flashscore.normalize_status`
    rather than trusted directly — an unmapped bucket must not look FINISHED.
    """

    SCHEDULED = "scheduled"
    LIVE = "live"
    FINISHED = "finished"
    POSTPONED = "postponed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class ExternalMixin:
    """Links a row back to the scraper's own identifier for a given source."""

    source: Mapped[str] = mapped_column(String(64), default="flashscore")
    external_id: Mapped[str] = mapped_column(String(128))


class Sport(Base):
    __tablename__ = "sports"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)

    tournaments: Mapped[list[Tournament]] = relationship(back_populates="sport")
    teams: Mapped[list[Team]] = relationship(back_populates="sport")


class Tournament(ExternalMixin, Base):
    __tablename__ = "tournaments"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)
    season: Mapped[str | None] = mapped_column(String(32), nullable=True)

    sport: Mapped[Sport] = relationship(back_populates="tournaments")
    matches: Mapped[list[Match]] = relationship(back_populates="tournament")


class Team(ExternalMixin, Base):
    __tablename__ = "teams"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    sport_id: Mapped[int] = mapped_column(ForeignKey("sports.id"))
    name: Mapped[str] = mapped_column(String(255))
    country: Mapped[str | None] = mapped_column(String(128), nullable=True)

    sport: Mapped[Sport] = relationship(back_populates="teams")
    player_links: Mapped[list[PlayerTeam]] = relationship(back_populates="team")


class Player(ExternalMixin, Base):
    __tablename__ = "players"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    birth_date: Mapped[dt.date | None] = mapped_column(nullable=True)
    position: Mapped[str | None] = mapped_column(String(64), nullable=True)

    team_links: Mapped[list[PlayerTeam]] = relationship(back_populates="player")


class PlayerTeam(Base):
    """Squad membership over time — a player can move between teams."""

    __tablename__ = "player_teams"

    id: Mapped[int] = mapped_column(primary_key=True)
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    shirt_number: Mapped[int | None] = mapped_column(nullable=True)
    valid_from: Mapped[dt.date | None] = mapped_column(nullable=True)
    valid_to: Mapped[dt.date | None] = mapped_column(nullable=True)

    player: Mapped[Player] = relationship(back_populates="team_links")
    team: Mapped[Team] = relationship(back_populates="player_links")


class Match(ExternalMixin, Base):
    __tablename__ = "matches"
    __table_args__ = (UniqueConstraint("source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"))
    home_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    away_team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    kickoff_at: Mapped[dt.datetime | None] = mapped_column(DateTime, nullable=True)
    round: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[MatchStatus] = mapped_column(
        Enum(MatchStatus, name="match_status"), default=MatchStatus.UNKNOWN
    )
    status_raw: Mapped[str | None] = mapped_column(String(64), nullable=True)
    home_score: Mapped[int | None] = mapped_column(nullable=True)
    away_score: Mapped[int | None] = mapped_column(nullable=True)
    periods: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    #: The match URL (the one you'd run `event` against), so odds-snapshot
    #: tooling (scripts/snapshot_odds.py) can re-scrape it without needing a
    #: separate team-slug lookup. Set by ingest_event.
    url: Mapped[str | None] = mapped_column(String(512), nullable=True)

    tournament: Mapped[Tournament] = relationship(back_populates="matches")
    home_team: Mapped[Team] = relationship(foreign_keys=[home_team_id])
    away_team: Mapped[Team] = relationship(foreign_keys=[away_team_id])
    team_stats: Mapped[list[MatchStat]] = relationship(back_populates="match")
    player_stats: Mapped[list[PlayerMatchStat]] = relationship(back_populates="match")
    odds: Mapped[list[Odds]] = relationship(back_populates="match")


class MatchStat(Base):
    """Team-level stat for a match, e.g. possession=54, corners=7, shots_on_target=5."""

    __tablename__ = "match_stats"
    __table_args__ = (UniqueConstraint("match_id", "team_id", "stat_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    stat_name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Numeric(10, 2))

    match: Mapped[Match] = relationship(back_populates="team_stats")
    team: Mapped[Team] = relationship()


class PlayerMatchStat(Base):
    """Player-level stat for a match, e.g. goals=1, assists=0, minutes_played=90."""

    __tablename__ = "player_match_stats"
    __table_args__ = (UniqueConstraint("match_id", "player_id", "stat_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    player_id: Mapped[int] = mapped_column(ForeignKey("players.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    stat_name: Mapped[str] = mapped_column(String(64))
    value: Mapped[float] = mapped_column(Numeric(10, 2))

    match: Mapped[Match] = relationship(back_populates="player_stats")
    player: Mapped[Player] = relationship()
    team: Mapped[Team] = relationship()


class Standing(Base):
    """
    One team's row in a tournament's league table at the time it was
    scraped. Flashscore's "standings" tab is a snapshot, not a time series
    the scraper exposes per-round — so this is upserted per (tournament,
    team) and simply reflects the latest scrape, not history.
    """

    __tablename__ = "standings"
    __table_args__ = (UniqueConstraint("tournament_id", "team_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"))
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    rank: Mapped[int | None] = mapped_column(nullable=True)
    played: Mapped[int | None] = mapped_column(nullable=True)
    wins: Mapped[int | None] = mapped_column(nullable=True)
    draws: Mapped[int | None] = mapped_column(nullable=True)
    losses: Mapped[int | None] = mapped_column(nullable=True)
    goals_for: Mapped[int | None] = mapped_column(nullable=True)
    goals_against: Mapped[int | None] = mapped_column(nullable=True)
    points: Mapped[int | None] = mapped_column(nullable=True)
    scraped_at: Mapped[dt.datetime] = mapped_column(DateTime)

    tournament: Mapped[Tournament] = relationship()
    team: Mapped[Team] = relationship()


class Odds(Base):
    """
    One odds quote at a point in time. Kept as a time series (not overwritten
    in place) so line movement is queryable — that movement is itself a
    betting signal, not just the closing line.
    """

    __tablename__ = "odds"
    __table_args__ = (Index("ix_odds_match_market_time", "match_id", "market", "recorded_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    bookmaker: Mapped[str] = mapped_column(String(64))
    market: Mapped[str] = mapped_column(String(64))  # e.g. "1x2", "over_under_2.5"
    selection: Mapped[str] = mapped_column(String(64))  # e.g. "home", "over"
    value: Mapped[float] = mapped_column(Numeric(6, 3))
    recorded_at: Mapped[dt.datetime] = mapped_column(DateTime)
    #: 't-24h' | 't-1h' | 'close' | None -- which pre-kickoff window
    #: scripts/snapshot_odds.py took this quote in, for CLV calculations.
    #: None for odds ingested without snapshot discipline (e.g. historical
    #: backfills, or the ad-hoc ingest-odds-cmd runs before this existed).
    snapshot: Mapped[str | None] = mapped_column(String(16), nullable=True)

    match: Mapped[Match] = relationship(back_populates="odds")
