"""
Idempotent importer: reads JSON files written by the external scraper and
upserts them into our own schema. Safe to re-run over the same files (and
over files the scraper re-wrote with updated live scores) — matching is by
(source, external_id), never by row id.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

from sqlalchemy.orm import Session

from newstats.db.models import (
    Match,
    MatchStat,
    MatchStatus,
    Odds,
    Player,
    PlayerMatchStat,
    PlayerTeam,
    Sport,
    Team,
    Tournament,
)
from newstats.importer.schema import MatchFile


def _get_or_create_sport(session: Session, name: str) -> Sport:
    sport = session.query(Sport).filter_by(name=name).one_or_none()
    if sport is None:
        sport = Sport(name=name)
        session.add(sport)
        session.flush()
    return sport


def _get_or_create_tournament(
    session: Session, source: str, sport: Sport, ref
) -> Tournament:
    tournament = (
        session.query(Tournament)
        .filter_by(source=source, external_id=ref.external_id)
        .one_or_none()
    )
    if tournament is None:
        tournament = Tournament(
            source=source,
            external_id=ref.external_id,
            sport_id=sport.id,
            name=ref.name,
            country=ref.country,
            season=ref.season,
        )
        session.add(tournament)
        session.flush()
    return tournament


def _get_or_create_team(session: Session, source: str, sport: Sport, ref) -> Team:
    team = (
        session.query(Team)
        .filter_by(source=source, external_id=ref.external_id)
        .one_or_none()
    )
    if team is None:
        team = Team(
            source=source,
            external_id=ref.external_id,
            sport_id=sport.id,
            name=ref.name,
            country=ref.country,
        )
        session.add(team)
        session.flush()
    return team


def _get_or_create_player(
    session: Session, source: str, team_by_ext: dict[str, Team], ref
) -> Player:
    player = (
        session.query(Player)
        .filter_by(source=source, external_id=ref.external_id)
        .one_or_none()
    )
    if player is None:
        player = Player(source=source, external_id=ref.external_id, name=ref.name)
        session.add(player)
        session.flush()

    team = team_by_ext[ref.team_external_id]
    link = (
        session.query(PlayerTeam)
        .filter_by(player_id=player.id, team_id=team.id)
        .one_or_none()
    )
    if link is None:
        session.add(PlayerTeam(player_id=player.id, team_id=team.id))
        session.flush()
    return player


def load_match_file(session: Session, path: Path) -> Match:
    payload = MatchFile.model_validate(json.loads(path.read_text()))

    sport = _get_or_create_sport(session, payload.sport)
    tournament = _get_or_create_tournament(session, payload.source, sport, payload.tournament)
    home_team = _get_or_create_team(session, payload.source, sport, payload.match.home_team)
    away_team = _get_or_create_team(session, payload.source, sport, payload.match.away_team)
    team_by_ext = {
        payload.match.home_team.external_id: home_team,
        payload.match.away_team.external_id: away_team,
    }

    match = (
        session.query(Match)
        .filter_by(source=payload.source, external_id=payload.match.external_id)
        .one_or_none()
    )
    kickoff_at = (
        dt.datetime.fromisoformat(payload.match.kickoff_at)
        if payload.match.kickoff_at
        else None
    )
    if match is None:
        match = Match(
            source=payload.source,
            external_id=payload.match.external_id,
            tournament_id=tournament.id,
            home_team_id=home_team.id,
            away_team_id=away_team.id,
        )
        session.add(match)

    match.kickoff_at = kickoff_at
    match.round = payload.match.round
    match.status = MatchStatus(payload.match.status)
    match.home_score = payload.match.home_score
    match.away_score = payload.match.away_score
    match.periods = payload.match.periods
    session.flush()

    for stat in payload.team_stats:
        team = team_by_ext[stat.team_external_id]
        row = (
            session.query(MatchStat)
            .filter_by(match_id=match.id, team_id=team.id, stat_name=stat.stat_name)
            .one_or_none()
        )
        if row is None:
            row = MatchStat(
                match_id=match.id, team_id=team.id, stat_name=stat.stat_name, value=stat.value
            )
            session.add(row)
        else:
            row.value = stat.value

    players_by_ext: dict[str, Player] = {}
    for player_ref in payload.players:
        players_by_ext[player_ref.external_id] = _get_or_create_player(
            session, payload.source, team_by_ext, player_ref
        )

    for stat in payload.player_stats:
        player = players_by_ext.get(stat.player_external_id)
        if player is None:
            # Stat references a player not listed in `players` for this file —
            # skip rather than guess an identity.
            continue
        team = team_by_ext[stat.team_external_id]
        row = (
            session.query(PlayerMatchStat)
            .filter_by(match_id=match.id, player_id=player.id, stat_name=stat.stat_name)
            .one_or_none()
        )
        if row is None:
            row = PlayerMatchStat(
                match_id=match.id,
                player_id=player.id,
                team_id=team.id,
                stat_name=stat.stat_name,
                value=stat.value,
            )
            session.add(row)
        else:
            row.value = stat.value

    for odds in payload.odds:
        session.add(
            Odds(
                match_id=match.id,
                bookmaker=odds.bookmaker,
                market=odds.market,
                selection=odds.selection,
                value=odds.value,
                recorded_at=dt.datetime.fromisoformat(odds.recorded_at),
            )
        )

    session.commit()
    return match


def load_directory(session: Session, directory: Path) -> int:
    count = 0
    for path in sorted(directory.glob("*.json")):
        load_match_file(session, path)
        count += 1
    return count
