"""
One-off bulk collection: last 10 results (all competitions) for all 36
Champions League 2026/27 league-phase teams, ingested into the newstats DB
via the already-built flashscore importer.

Uses the FlashscoreScraper repo's own parser code (imported, not modified)
to parse match pages the exact same way `main.py event <url>` does, so the
resulting dicts match what `newstats.importer.flashscore.ingest_event`
expects. Team results-list pages (`/team/<slug>/<id>/results/`) aren't
something the scraper CLI handles (it's per-match-URL only), so this script
parses those itself with BeautifulSoup to discover each team's last 10
match URLs, then feeds each unique match URL through the real parser.

Requires a local clone of M3MONs/FlashscoreScraper with its own venv (playwright,
beautifulsoup4) set up -- point FLASHSCORE_SCRAPER at it.

Run: FLASHSCORE_SCRAPER=/path/to/FlashscoreScraper DATABASE_URL=... \
     python scripts/collect_cl_teams.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import asdict
from pathlib import Path

SCRAPER_REPO = os.environ.get("FLASHSCORE_SCRAPER")
if not SCRAPER_REPO:
    raise SystemExit("Set FLASHSCORE_SCRAPER to a local clone of M3MONs/FlashscoreScraper")
NEWSTATS_REPO = os.environ.get("NEWSTATS_REPO", str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, SCRAPER_REPO)
sys.path.insert(0, NEWSTATS_REPO)

from bs4 import BeautifulSoup
from parsers.football.football_event_parser import FootballEventParser
from playwright.async_api import async_playwright

from newstats.db.session import SessionLocal
from newstats.importer.flashscore import ingest_event

TEAMS = [
    ("Barcelona", "barcelona", "SKbpVP5K"),
    ("Sporting CP", "sporting-cp", "tljXuHBC"),
    ("Arsenal", "arsenal", "hA1Zm19f"),
    ("Bayern Munich", "bayern-munich", "nVp0wiqd"),
    ("PSG", "psg", "CjhkPw0k"),
    ("Como", "como", "ttyLthOA"),
    ("Manchester Utd", "manchester-united", "ppjDR086"),
    ("Stuttgart", "vfb-stuttgart", "nJQmYp1B"),
    ("Manchester City", "manchester-city", "Wtn9Stg0"),
    ("Villarreal", "villarreal", "lUatW5jE"),
    ("Aston Villa", "aston-villa", "W00wmLO0"),
    ("Betis", "betis", "vJbTeCGP"),
    ("Atl. Madrid", "atl-madrid", "jaarqpLQ"),
    ("Dortmund", "dortmund", "nP1i5US1"),
    ("Liverpool", "liverpool", "lId4TMwf"),
    ("Real Madrid", "real-madrid", "W8mj7MDD"),
    ("AEK Athens", "aek", "ANpZncAM"),
    ("Club Brugge KV", "club-brugge", "rgTHIK74"),
    ("Lens", "lens", "IBmris38"),
    ("RB Leipzig", "rb-leipzig", "KbS1suSm"),
    ("Sabah Baku", "sabah-baku", "fNGcxbyr"),
    ("AS Roma", "as-roma", "zVqqL0ma"),
    ("Shakhtar Donetsk", "shakhtar", "4ENWX2OA"),
    ("Fenerbahce", "fenerbahce", "MsbmracL"),
    ("PSV", "psv", "M9UEHJWi"),
    ("Viking", "viking", "bXAgOWwb"),
    ("LASK", "lask-linz", "MipWYeKQ"),
    ("Inter", "inter", "Iw7eKK25"),
    ("FC Porto", "fc-porto", "S2NmScGp"),
    ("Slavia Prague", "slavia-prague", "viXGgnyB"),
    ("Napoli", "napoli", "69Dxbc61"),
    ("Lille", "lille", "pfDZL71o"),
    ("Feyenoord", "feyenoord", "8zjySeoN"),
    ("Galatasaray", "galatasaray", "riaqqurF"),
    ("Slovan Bratislava", "slovan-bratislava", "QRaWdwQf"),
    ("Bodo/Glimt", "bodo-glimt", "S0WZMUNG"),
]

MAX_CONCURRENT = 5
MATCHES_PER_TEAM = 10


async def fetch_html(context, url: str, timeout_ms: int = 20000) -> str:
    page = await context.new_page()
    try:
        await page.goto(url, wait_until="networkidle", timeout=timeout_ms)
        return await page.content()
    finally:
        await page.close()


async def collect_team_matches(context, sem, name, slug, team_id) -> tuple[str, list[dict], str | None]:
    url = f"https://www.flashscore.com/team/{slug}/{team_id}/results/"
    async with sem:
        try:
            html = await fetch_html(context, url)
        except Exception as e:  # noqa: BLE001
            return name, [], f"results page failed: {e}"

    soup = BeautifulSoup(html, "html.parser")
    rows = soup.find_all("div", class_="event__match")[:MATCHES_PER_TEAM]
    matches = []
    for row in rows:
        link_el = row.find("a", class_="eventRowLink")
        if not link_el or not link_el.get("href"):
            continue
        matches.append({"match_url": link_el["href"], "row_id": row.get("id")})
    return name, matches, None


async def fetch_and_parse_match(context, sem, match_url: str) -> tuple[str, dict | None, str | None]:
    async with sem:
        try:
            html = await fetch_html(context, match_url)
        except Exception as e:  # noqa: BLE001
            return match_url, None, f"page fetch failed: {e}"

    soup = BeautifulSoup(html, "html.parser")
    try:
        event = FootballEventParser.parse_event(soup)
    except Exception as e:  # noqa: BLE001
        return match_url, None, f"parse failed: {e}"
    return match_url, asdict(event), None


async def main() -> None:
    sem = asyncio.Semaphore(MAX_CONCURRENT)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--disable-gpu", "--disable-dev-shm-usage", "--disable-extensions"],
        )
        context = await browser.new_context()

        print(f"--- discovering last {MATCHES_PER_TEAM} matches for {len(TEAMS)} teams ---", flush=True)
        results = await asyncio.gather(
            *[collect_team_matches(context, sem, name, slug, tid) for name, slug, tid in TEAMS]
        )

        unique_matches: dict[str, str] = {}
        team_errors = []
        for name, matches, err in results:
            if err:
                team_errors.append((name, err))
                print(f"  [team error] {name}: {err}", flush=True)
                continue
            print(f"  {name}: {len(matches)} matches found", flush=True)
            for m in matches:
                unique_matches[m["row_id"] or m["match_url"]] = m["match_url"]

        print(f"--- {len(unique_matches)} unique match URLs to fetch ---", flush=True)

        match_results = await asyncio.gather(
            *[fetch_and_parse_match(context, sem, url) for url in unique_matches.values()]
        )

        await browser.close()

    session = SessionLocal()
    ingested, match_errors = 0, []
    try:
        for match_url, event, err in match_results:
            if err or event is None:
                match_errors.append((match_url, err))
                continue
            try:
                ingest_event(session, match_url, event)
                ingested += 1
            except Exception as e:  # noqa: BLE001
                match_errors.append((match_url, f"ingest failed: {e}"))
    finally:
        session.close()

    print(f"--- done: {ingested} matches ingested, {len(match_errors)} match errors, {len(team_errors)} team errors ---", flush=True)
    for url, err in match_errors:
        print(f"  [match error] {url}: {err}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
