"""
Second pass over matches already in the DB (via ingest_event / collect_cl_teams.py):
backfills kickoff_at, and ingests per-match team stats (corners, fouls, ...)
and player events (goals, assists, cards) by scraping each match's summary
page (incident timeline) and its ".../summary/stats/overall/" sub-page
directly with Playwright + BeautifulSoup -- FlashscoreScraper's CLI has no
concept of either, see newstats/importer/flashscore.py's module docstring.

By default only fetches matches that don't have team stats yet (no
MatchStat row at all for them) -- re-scraping all 300+ matches on every
run is slow and pointless once they're already populated. Pass --all to
force a full refresh (e.g. after a parsing-logic change).

Run from the repo root: DATABASE_URL=... .venv/bin/python scripts/collect_match_details.py
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from pathlib import Path

NEWSTATS_REPO = os.environ.get("NEWSTATS_REPO", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, NEWSTATS_REPO)

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from newstats.db.models import Match, MatchStat
from newstats.db.session import SessionLocal
from newstats.importer.flashscore import (
    ingest_match_events,
    ingest_match_stats,
    parse_kickoff,
)

MAX_CONCURRENT = 5


def build_match_url(m: Match) -> str:
    """Prefer the URL ingest_event stored; reconstruct only for rows ingested before Match.url existed."""
    if m.url:
        return m.url
    home_slug = m.home_team.external_id.strip("/").split("/")[-2]
    home_id = m.home_team.external_id.strip("/").split("/")[-1]
    away_slug = m.away_team.external_id.strip("/").split("/")[-2]
    away_id = m.away_team.external_id.strip("/").split("/")[-1]
    return f"https://www.flashscore.com/match/football/{home_slug}-{home_id}/{away_slug}-{away_id}/?mid={m.external_id}"


async def fetch_html(context, url: str, timeout_ms: int = 20000) -> str:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        await page.wait_for_timeout(500)
        return await page.content()
    finally:
        await page.close()


def parse_summary(html: str) -> tuple[str | None, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")

    date_el = soup.find("div", class_="duelParticipant__startTime")
    kickoff_raw = date_el.get_text(strip=True) if date_el else None

    incidents = []
    for inc in soup.select(".smv__incident"):
        side_parent = inc.find_parent(class_=re.compile("smv__participantRow"))
        side = None
        if side_parent:
            classes = side_parent.get("class") or []
            if "smv__homeParticipant" in classes:
                side = "home"
            elif "smv__awayParticipant" in classes:
                side = "away"

        icon_classes = set()
        for child in inc.select(".smv__incidentIcon, .smv__incidentIconSub"):
            icon_classes.update(child.get("class") or [])
            for sub in child.find_all(True):
                icon_classes.update(sub.get("class") or [])

        if "yellowCard-ico" in icon_classes:
            inc_type = "yellow_card"
        elif "redCard-ico" in icon_classes:
            inc_type = "red_card"
        elif "incidents-goal-soccer" in icon_classes:
            inc_type = "goal"
        else:
            inc_type = None

        minute_el = inc.find(class_="smv__timeBox")
        minute = minute_el.get_text(strip=True) if minute_el else None

        player_el = inc.select_one("a.smv__playerName")
        player_name = player_el.get_text(strip=True) if player_el else None
        player_link = player_el.get("href") if player_el else None

        if inc_type and player_link and player_name:
            incidents.append(
                {"type": inc_type, "minute": minute, "side": side, "player_name": player_name, "player_link": player_link}
            )

        if inc_type == "goal":
            assist_el = inc.select_one(".smv__assist a")
            if assist_el and assist_el.get("href"):
                incidents.append(
                    {
                        "type": "assist",
                        "minute": minute,
                        "side": side,
                        "player_name": assist_el.get_text(strip=True),
                        "player_link": assist_el.get("href"),
                    }
                )

    return kickoff_raw, incidents


def parse_stats(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    rows = soup.select('[data-testid="wcl-statistics"]')
    seen: dict[str, dict] = {}
    for r in rows:
        label_el = r.select_one(".wcl-label_sO4bA span, .wcl-label_sO4bA h4")
        label = label_el.get_text(strip=True) if label_el else None
        if not label or label in seen:
            continue
        home_el = r.select_one(".wcl-labelRow_42JBQ > div:first-child")
        away_el = r.select_one(".wcl-awayValue_smmfR")
        seen[label] = {
            "label": label,
            "home": home_el.get_text(" ", strip=True) if home_el else None,
            "away": away_el.get_text(" ", strip=True) if away_el else None,
        }
    return list(seen.values())


async def fetch_match_details(context, sem, match_id: int, match_url: str) -> dict:
    async with sem:
        try:
            summary_html = await fetch_html(context, match_url)
        except Exception as e:  # noqa: BLE001
            return {"match_id": match_id, "error": f"summary fetch failed: {e}"}
    kickoff_raw, incidents = parse_summary(summary_html)

    stats_url = match_url.replace("/?mid=", "/summary/stats/overall/?mid=")
    async with sem:
        try:
            stats_html = await fetch_html(context, stats_url)
        except Exception as e:  # noqa: BLE001
            return {"match_id": match_id, "kickoff_raw": kickoff_raw, "incidents": incidents, "stats": [], "error": f"stats fetch failed: {e}"}
    stats = parse_stats(stats_html)

    return {"match_id": match_id, "kickoff_raw": kickoff_raw, "incidents": incidents, "stats": stats, "error": None}


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--all", action="store_true", help="Re-fetch every match, including ones that already have stats")
    return p.parse_args()


async def main() -> None:
    args = parse_args()
    session = SessionLocal()
    query = session.query(Match)
    if not args.all:
        already_done = {row[0] for row in session.query(MatchStat.match_id).distinct()}
        query = query.filter(~Match.id.in_(already_done)) if already_done else query
    matches = query.all()
    targets = [(m.id, build_match_url(m)) for m in matches]
    session.close()

    print(f"--- fetching details for {len(targets)} matches ---", flush=True)
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True, args=["--disable-gpu", "--disable-dev-shm-usage", "--disable-extensions"])
        context = await browser.new_context(timezone_id="UTC")
        results = await asyncio.gather(*[fetch_match_details(context, sem, mid, url) for mid, url in targets])
        await browser.close()

    session = SessionLocal()
    ok, errors = 0, []
    total_stats, total_events = 0, 0
    try:
        for r, (mid, url) in zip(results, targets):
            if r.get("error") and not r.get("incidents") and not r.get("stats"):
                errors.append((mid, r["error"]))
                continue
            try:
                m = session.query(Match).filter_by(id=mid).one()
                kickoff = parse_kickoff(r.get("kickoff_raw"), source_tz="UTC")  # browser context is pinned to timezone_id="UTC"
                if kickoff:
                    m.kickoff_at = kickoff
                    session.commit()
                stat_rows = ingest_match_stats(session, url, r.get("stats") or [])
                event_rows = ingest_match_events(session, url, r.get("incidents") or [])
                total_stats += len(stat_rows)
                total_events += len(event_rows)
                ok += 1
            except Exception as e:  # noqa: BLE001
                errors.append((mid, f"ingest failed: {e}"))
    finally:
        session.close()

    print(f"--- done: {ok} matches processed, {total_stats} stat rows, {total_events} player-stat rows, {len(errors)} errors ---", flush=True)
    for mid, err in errors[:30]:
        print(f"  [error] match {mid}: {err}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
