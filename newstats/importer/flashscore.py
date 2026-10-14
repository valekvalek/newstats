"""
Adapter for the real output of M3MONs/FlashscoreScraper
(https://github.com/M3MONs/FlashscoreScraper), confirmed against its source
(models/event_data.py, models/odds_parser_row.py,
models/event_info/football_event_info.py as of 2026-10).

That tool is a per-URL CLI, invoked three separate ways per match, each
printing one JSON object to stdout — it does not batch multiple matches into
one file and does not write files itself:

  python main.py event      <match_url>   -> EventData
  python main.py odds       <match_url>   -> OddsResult   (one call per odds type)
  python main.py event-info <match_url>/standings/standings/overall/  -> EventInfoData
  python main.py event-info <match_url>/draw/                         -> EventInfoData

It has no concept of players or team-level in-match stats (possession,
corners, ...) — those fields simply don't exist in this source. It also
never emits a stable numeric match id as a JSON field: the id lives in the
match URL's `?mid=` query parameter (or, for older links, in the URL path),
so `match_external_id` below extracts it from the URL you scraped, not from
the JSON body.

Real shapes (field names and nesting as the dataclasses actually serialize):

EventData (the `event` command):
{
  "date": "05.05.2026 18:00",
  "participants": [
    {"role": "home", "name": "Team A", "img": "...", "link": "/team/team-a-AbC123/"},
    {"role": "away", "name": "Team B", "img": "...", "link": "/team/team-b-XyZ987/"}
  ],
  "score": "2 - 1",
  "detail_status": "Finished",
  "league": {"name": "Premier League", "link": "/football/england/premier-league/"}
}

OddsResult (the `odds` command, one call per odds type e.g. "1x2-odds"):
{
  "url": "https://www.flashscore.com/match/football/team-a-.../team-b-.../odds/1x2-odds?mid=...",
  "odd_type": "1x2-odds",
  "data": [
    {"bookmaker_id": "2", "bookmaker_name": "Bet365", "bookmaker_link": "...",
     "odds_values": [{"value": "1", "type": "Home", "odds": "1.85"}, ...]}
  ],
  "error": null
}

EventInfoData (the `event-info` command, standings variant):
{
  "info_type": "standings",
  "data": [
    {"rank": 1, "promotion_status": null, "team": "Team A", "team_url": "/team/team-a-AbC123/",
     "matches_played": "10", "wins": "7", "draws": "2", "losses": "1",
     "goals_for": "20", "goals_against": "8", "goal_difference": "+12", "points": "23",
     "form": [{"result": "W", "match_url": "..."}], "highlighted": false}
  ],
  "metadata": {}
}

The scraper never timestamps its own output — a quote or a standings row is
only ever "what the page showed when we scraped it" — so every ingest
function here takes an explicit `scraped_at`, supplied by the caller at the
moment the scrape ran, rather than inventing a field the source doesn't have.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Any
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from newstats.db.models import (
    Match,
    MatchStat,
    MatchStatus,
    Odds,
    Player,
    PlayerMatchStat,
    Sport,
    Standing,
    Team,
    Tournament,
)

_STATUS_MAP: dict[str, MatchStatus] = {
    "finished": MatchStatus.FINISHED,
    # Confirmed live (not "After ET"/"After Pen." as originally assumed from the
    # docstring example -- see README "Известные ограничения"): "after extra time"
    # carries the full 120-minute score, safe to treat as a normal result. "after
    # 90 minutes" is the penalty-shootout case -- `score` is the PRE-shootout
    # score, so who actually won isn't recoverable from this field; still FINISHED
    # (the match is over), but callers that care about the 1X2 outcome specifically
    # (e.g. scripts/backtest.py) should treat this status_raw as ambiguous and skip it.
    "after extra time": MatchStatus.FINISHED,
    "after 90 minutes": MatchStatus.FINISHED,
    "after pen.": MatchStatus.FINISHED,
    "after et": MatchStatus.FINISHED,
    "postponed": MatchStatus.POSTPONED,
    "postp.": MatchStatus.POSTPONED,
    "cancelled": MatchStatus.CANCELLED,
    "canceled": MatchStatus.CANCELLED,
    "abandoned": MatchStatus.CANCELLED,
    "awaiting updates": MatchStatus.SCHEDULED,
    "scheduled": MatchStatus.SCHEDULED,
}

_SCORE_RE = re.compile(r"(\d+)\s*-\s*(\d+)")


def normalize_status(detail_status: str | None) -> MatchStatus:
    # Confirmed live: a not-yet-started match's detail_status is "" (empty),
    # not absent/None and not the literal "Scheduled" the original mapping
    # assumed -- every upcoming match looks like this before kickoff.
    if detail_status is None:
        return MatchStatus.UNKNOWN
    if detail_status.strip() == "":
        return MatchStatus.SCHEDULED
    key = detail_status.strip().lower()
    if key in _STATUS_MAP:
        return _STATUS_MAP[key]
    if "live" in key or re.fullmatch(r"\d{1,3}'?", key):
        return MatchStatus.LIVE
    return MatchStatus.UNKNOWN


def is_penalty_decided(status_raw: str | None) -> bool:
    """
    True when `score` is known to be the PRE-shootout score (see
    README "Известные ограничения") -- the match is FINISHED, but its 1X2
    outcome as stored (home_score/away_score) does not reflect who actually
    advanced/won. Callers that care about the match RESULT, not just the
    goals scored, should exclude these rather than silently treating a
    penalty-shootout loss as a draw.
    """
    return (status_raw or "").strip().lower() == "after 90 minutes"


def parse_score(score: str | None) -> tuple[int | None, int | None]:
    """'2 - 1' -> (2, 1). Anything else (e.g. 'Awaiting updates') -> (None, None)."""
    if not score:
        return None, None
    match = _SCORE_RE.search(score)
    if not match:
        return None, None
    return int(match.group(1)), int(match.group(2))


#: Flashscore renders `event["date"]` in whichever timezone the scraping browser
#: is in (client-side JS, see README "Известные ограничения") -- not UTC and not
#: a fixed site-wide zone. Set SOURCE_TZ to match the machine running the
#: scraper; confirmed live as Europe/Moscow for this project's own scraping host.
SOURCE_TZ = ZoneInfo(os.environ.get("SOURCE_TZ", "UTC"))


def parse_kickoff(date_str: str | None) -> dt.datetime | None:
    """'13.10.2026 22:00' in SOURCE_TZ local time -> naive UTC datetime."""
    if not date_str:
        return None
    try:
        naive_local = dt.datetime.strptime(date_str.strip(), "%d.%m.%Y %H:%M")
    except ValueError:
        return None
    return naive_local.replace(tzinfo=SOURCE_TZ).astimezone(dt.UTC).replace(tzinfo=None)


def extract_match_id(match_url: str) -> str:
    """
    Prefer the `mid` query param (the real Flashscore match id, present on
    links copied from the site). Fall back to the URL path with any
    trailing sub-page (odds/..., draw/, standings/...) stripped, for the
    rare link that lacks `mid` — still stable across re-scrapes of the same
    match, just less canonical than the real id.
    """
    parsed = urlparse(match_url)
    mid = parse_qs(parsed.query).get("mid")
    if mid:
        return mid[0]
    path = parsed.path.rstrip("/")
    path = re.sub(r"/(odds/[^/]+|draw|standings(/.*)?)$", "", path)
    return path


def _get_or_create_sport(session: Session, name: str) -> Sport:
    sport = session.query(Sport).filter_by(name=name).one_or_none()
    if sport is None:
        sport = Sport(name=name)
        session.add(sport)
        session.flush()
    return sport


def _get_or_create_tournament(session: Session, sport: Sport, league: dict[str, Any] | None) -> Tournament:
    link = (league or {}).get("link") or "unknown-league"
    name = (league or {}).get("name") or "Unknown league"
    tournament = session.query(Tournament).filter_by(source="flashscore", external_id=link).one_or_none()
    if tournament is None:
        tournament = Tournament(source="flashscore", external_id=link, sport_id=sport.id, name=name)
        session.add(tournament)
        session.flush()
    return tournament


def _get_or_create_team(session: Session, sport: Sport, external_id: str, name: str) -> Team:
    team = session.query(Team).filter_by(source="flashscore", external_id=external_id).one_or_none()
    if team is None:
        team = Team(source="flashscore", external_id=external_id, sport_id=sport.id, name=name)
        session.add(team)
        session.flush()
    elif team.name != name:
        team.name = name  # sites rename/re-slug teams occasionally; keep the latest
    return team


def ingest_event(session: Session, match_url: str, event: dict[str, Any], sport_name: str = "football") -> Match:
    sport = _get_or_create_sport(session, sport_name)
    tournament = _get_or_create_tournament(session, sport, event.get("league"))

    participants = {p["role"]: p for p in event["participants"]}
    home_ref, away_ref = participants["home"], participants["away"]
    home_team = _get_or_create_team(session, sport, home_ref["link"], home_ref["name"])
    away_team = _get_or_create_team(session, sport, away_ref["link"], away_ref["name"])

    match_id = extract_match_id(match_url)
    match = session.query(Match).filter_by(source="flashscore", external_id=match_id).one_or_none()
    if match is None:
        match = Match(
            source="flashscore",
            external_id=match_id,
            tournament_id=tournament.id,
            home_team_id=home_team.id,
            away_team_id=away_team.id,
        )
        session.add(match)

    match.tournament_id = tournament.id
    match.home_team_id = home_team.id
    match.away_team_id = away_team.id
    match.url = match_url
    match.status_raw = event.get("detail_status")
    match.status = normalize_status(event.get("detail_status"))
    match.home_score, match.away_score = parse_score(event.get("score"))
    match.kickoff_at = parse_kickoff(event.get("date"))
    session.commit()
    return match


def ingest_odds(
    session: Session,
    match_url: str,
    odds_result: dict[str, Any],
    scraped_at: dt.datetime,
    snapshot: str | None = None,
) -> list[Odds]:
    """
    `match_url` is the URL you ran `event` against for this match, same as
    every other ingest_* function here -- NOT `odds_result["url"]`. Confirmed
    live against the real scraper: `FlashscoreUrlBuilder.build_odds_url`
    strips the `?mid=` query param when it rebuilds the per-odds-type URL, so
    `odds_result["url"]` almost never carries the same external_id the match
    was originally ingested under. Deriving the match purely from that field
    (as an earlier version of this function did) silently failed to find the
    match on real data despite working against the fixtures.

    `snapshot` tags which pre-kickoff window this quote belongs to
    ('t-24h' | 't-1h' | 'close'), for CLV calculations -- leave it unset
    for ad-hoc scrapes with no snapshot discipline. See scripts/snapshot_odds.py.
    """
    if odds_result.get("error"):
        return []

    match_id = extract_match_id(match_url)
    match = session.query(Match).filter_by(source="flashscore", external_id=match_id).one_or_none()
    if match is None:
        raise ValueError(
            f"No match with external_id={match_id!r} — run ingest_event for this match before ingest_odds"
        )

    market = odds_result["odd_type"]
    rows: list[Odds] = []
    for bookmaker_row in odds_result.get("data") or []:
        for value in bookmaker_row["odds_values"]:
            try:
                price = float(value["odds"])
            except (TypeError, ValueError):
                continue
            row = Odds(
                match_id=match.id,
                bookmaker=bookmaker_row["bookmaker_name"],
                market=market,
                selection=value["type"],
                value=price,
                recorded_at=scraped_at,
                snapshot=snapshot,
            )
            session.add(row)
            rows.append(row)
    session.commit()
    return rows


def ingest_standings(
    session: Session, match_url: str, event_info: dict[str, Any], scraped_at: dt.datetime
) -> list[Standing]:
    if event_info.get("info_type") != "standings":
        raise ValueError(f"expected info_type='standings', got {event_info.get('info_type')!r}")

    match_id = extract_match_id(match_url)
    match = session.query(Match).filter_by(source="flashscore", external_id=match_id).one_or_none()
    if match is None:
        raise ValueError(
            f"No match with external_id={match_id!r} — run ingest_event for this match before ingest_standings"
        )
    tournament = match.tournament
    sport = tournament.sport

    def _int(value: Any) -> int | None:
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return None

    rows: list[Standing] = []
    for entry in event_info.get("data") or []:
        team_url = entry.get("team_url")
        team_name = entry.get("team")
        if not team_url or not team_name:
            continue
        team = _get_or_create_team(session, sport, team_url, team_name)

        standing = (
            session.query(Standing)
            .filter_by(tournament_id=tournament.id, team_id=team.id)
            .one_or_none()
        )
        if standing is None:
            standing = Standing(tournament_id=tournament.id, team_id=team.id)
            session.add(standing)

        standing.rank = _int(entry.get("rank"))
        standing.played = _int(entry.get("matches_played"))
        standing.wins = _int(entry.get("wins"))
        standing.draws = _int(entry.get("draws"))
        standing.losses = _int(entry.get("losses"))
        standing.goals_for = _int(entry.get("goals_for"))
        standing.goals_against = _int(entry.get("goals_against"))
        standing.points = _int(entry.get("points"))
        standing.scraped_at = scraped_at
        session.flush()
        rows.append(standing)

    session.commit()
    return rows


# ---------------------------------------------------------------------------
# Match stats and player events.
#
# These two functions consume data M3MONs/FlashscoreScraper's CLI does NOT
# expose at all (see module docstring: "no concept of players or team-level
# in-match stats"). Corners/fouls/possession live on a match's own
# ".../summary/stats/overall/" sub-page, and goals/cards/substitutions live
# as a timeline of "incident" rows on the plain match summary page — both
# require rendering the page's JS (Flashscore ships almost no stats in the
# static HTML), so they're scraped directly with Playwright + BeautifulSoup,
# not via the FlashscoreScraper package. The shapes below are what that
# direct scrape produces; see scratchpad/collect_cl_match_details.py for the
# actual scraper that builds them.
#
# stat entry shape: {"label": "Corner kicks", "home": "2", "away": "5"}
# incident entry shape: {"type": "goal"|"yellow_card"|"red_card", "minute": "1'",
#                         "side": "home"|"away", "player_name": "Barrios P.",
#                         "player_link": "/player/barrios-pablo/AaZiGJ9E/"}
# ---------------------------------------------------------------------------

#: Flashscore stat labels we care about -> our own stat_name. Any label not
#: listed here is still stored (slugified) so nothing is silently dropped.
_STAT_LABEL_OVERRIDES = {
    "Corner kicks": "corners",
    "Fouls": "fouls",
    "Ball possession": "possession_pct",
    "Yellow Cards": "yellow_cards",
    "Red Cards": "red_cards",
    "Offsides": "offsides",
}


def _slugify_stat_label(label: str) -> str:
    return _STAT_LABEL_OVERRIDES.get(label) or re.sub(r"[^a-z0-9]+", "_", label.lower()).strip("_")


def _parse_stat_value(raw: str | None) -> float | None:
    """'285 (81%)' -> 285.0, '35%' -> 35.0, '0.28' -> 0.28, '' / None -> None."""
    if not raw:
        return None
    match = re.search(r"-?\d+(?:\.\d+)?", raw)
    return float(match.group()) if match else None


def ingest_match_stats(session: Session, match_url: str, stats: list[dict[str, Any]]) -> list[MatchStat]:
    match_id = extract_match_id(match_url)
    match = session.query(Match).filter_by(source="flashscore", external_id=match_id).one_or_none()
    if match is None:
        raise ValueError(
            f"No match with external_id={match_id!r} — run ingest_event for this match before ingest_match_stats"
        )

    rows: list[MatchStat] = []
    for entry in stats:
        label = entry.get("label")
        if not label:
            continue
        stat_name = _slugify_stat_label(label)
        for side, team_id in (("home", match.home_team_id), ("away", match.away_team_id)):
            value = _parse_stat_value(entry.get(side))
            if value is None:
                continue
            row = (
                session.query(MatchStat)
                .filter_by(match_id=match.id, team_id=team_id, stat_name=stat_name)
                .one_or_none()
            )
            if row is None:
                row = MatchStat(match_id=match.id, team_id=team_id, stat_name=stat_name)
                session.add(row)
            row.value = value
            session.flush()
            rows.append(row)

    session.commit()
    return rows


_INCIDENT_STAT_NAMES = {
    "goal": "goals",
    "assist": "assists",
    "yellow_card": "yellow_cards",
    "red_card": "red_cards",
}


def ingest_match_events(session: Session, match_url: str, incidents: list[dict[str, Any]]) -> list[PlayerMatchStat]:
    match_id = extract_match_id(match_url)
    match = session.query(Match).filter_by(source="flashscore", external_id=match_id).one_or_none()
    if match is None:
        raise ValueError(
            f"No match with external_id={match_id!r} — run ingest_event for this match before ingest_match_events"
        )

    # Pre-aggregate: a player can score twice, so counts (not last-write) matter.
    counts: dict[tuple[str, str, int], int] = {}  # (player_link, player_name, team_id) -> count
    for inc in incidents:
        stat_name = _INCIDENT_STAT_NAMES.get(inc.get("type", ""))
        if stat_name is None:
            continue
        player_link = inc.get("player_link")
        player_name = inc.get("player_name")
        if not player_link or not player_name:
            continue
        team_id = match.home_team_id if inc.get("side") == "home" else match.away_team_id
        key = (player_link, stat_name, team_id)
        counts[key] = counts.get(key, 0) + 1

    rows: list[PlayerMatchStat] = []
    for (player_link, stat_name, team_id), count in counts.items():
        player_name = next(
            inc["player_name"]
            for inc in incidents
            if inc.get("player_link") == player_link and inc.get("player_name")
        )
        player = session.query(Player).filter_by(source="flashscore", external_id=player_link).one_or_none()
        if player is None:
            player = Player(source="flashscore", external_id=player_link, name=player_name)
            session.add(player)
            session.flush()
        elif player.name != player_name:
            player.name = player_name

        row = (
            session.query(PlayerMatchStat)
            .filter_by(match_id=match.id, player_id=player.id, stat_name=stat_name)
            .one_or_none()
        )
        if row is None:
            row = PlayerMatchStat(match_id=match.id, player_id=player.id, team_id=team_id, stat_name=stat_name)
            session.add(row)
        row.team_id = team_id
        row.value = float(count)
        session.flush()
        rows.append(row)

    session.commit()
    return rows
