"""
Expected shape of one scraper output file (one JSON per match).

This is a *contract*, not something read from the scraper's real output yet —
adjust field names in `load_match_file` once you point this at actual
FlashscoreScrapper (or whichever tool) output. Keeping the contract in one
place means that adjustment is a one-file change.

{
  "source": "flashscore",
  "sport": "football",
  "tournament": {"external_id": "...", "name": "...", "country": "...", "season": "..."},
  "match": {
    "external_id": "...",
    "kickoff_at": "2026-05-05T18:00:00",
    "round": "Round 12",
    "status": "finished",
    "home_team": {"external_id": "...", "name": "...", "country": "..."},
    "away_team": {"external_id": "...", "name": "...", "country": "..."},
    "home_score": 2,
    "away_score": 1,
    "periods": {"1h": [1, 0], "2h": [1, 1]}
  },
  "team_stats": [
    {"team_external_id": "...", "stat_name": "possession", "value": 54.0}
  ],
  "players": [
    {"external_id": "...", "name": "...", "team_external_id": "...", "position": "FW"}
  ],
  "player_stats": [
    {"player_external_id": "...", "team_external_id": "...", "stat_name": "goals", "value": 1}
  ],
  "odds": [
    {"bookmaker": "...", "market": "1x2", "selection": "home", "value": 1.85,
     "recorded_at": "2026-05-05T12:00:00"}
  ]
}
"""

from __future__ import annotations

from pydantic import BaseModel


class TeamRef(BaseModel):
    external_id: str
    name: str
    country: str | None = None


class TournamentRef(BaseModel):
    external_id: str
    name: str
    country: str | None = None
    season: str | None = None


class MatchPayload(BaseModel):
    external_id: str
    kickoff_at: str | None = None
    round: str | None = None
    status: str = "scheduled"
    home_team: TeamRef
    away_team: TeamRef
    home_score: int | None = None
    away_score: int | None = None
    periods: dict | None = None


class TeamStatPayload(BaseModel):
    team_external_id: str
    stat_name: str
    value: float


class PlayerPayload(BaseModel):
    external_id: str
    name: str
    team_external_id: str
    position: str | None = None


class PlayerStatPayload(BaseModel):
    player_external_id: str
    team_external_id: str
    stat_name: str
    value: float


class OddsPayload(BaseModel):
    bookmaker: str
    market: str
    selection: str
    value: float
    recorded_at: str


class MatchFile(BaseModel):
    source: str = "flashscore"
    sport: str
    tournament: TournamentRef
    match: MatchPayload
    team_stats: list[TeamStatPayload] = []
    players: list[PlayerPayload] = []
    player_stats: list[PlayerStatPayload] = []
    odds: list[OddsPayload] = []
