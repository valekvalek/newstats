"""
Take odds snapshots at fixed windows before kickoff (t-24h, t-1h, close) for
every SCHEDULED match kicking off in the next ~25 hours, so CLV (closing
line value) is computable later -- without snapshot discipline there is no
way to know what price was actually available when, and "model vs market"
comparisons are just noise.

Calls the real FlashscoreScraper CLI (main.py odds <url>) as a subprocess
-- not the Playwright scrape this project uses elsewhere -- because odds
parsing/discovery (which bookmakers, which odds types exist) is exactly
what that package already does. Requires FLASHSCORE_SCRAPER pointing at a
local clone with its own venv set up (see scripts/collect_cl_teams.py).

Intended to run under cron every ~5 minutes on a machine that's always on
-- GitHub Actions won't work here, it needs a live Playwright browser and
a persistent DB connection, not a one-shot CI job.

    */5 * * * * cd /path/to/newstats && \
        FLASHSCORE_SCRAPER=/path/to/FlashscoreScraper DATABASE_URL=... \
        .venv/bin/python scripts/snapshot_odds.py >> snapshot_odds.log 2>&1

Idempotent: a (match, snapshot window) pair that's already been recorded
is skipped, so re-running (or overlapping cron ticks) never double-snapshots.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("NEWSTATS_REPO", str(Path(__file__).resolve().parent.parent)))

from newstats.db.models import Match, MatchStatus, Odds
from newstats.db.session import SessionLocal
from newstats.importer.flashscore import ingest_odds

SCRAPER_REPO = os.environ.get("FLASHSCORE_SCRAPER")
if not SCRAPER_REPO:
    raise SystemExit("Set FLASHSCORE_SCRAPER to a local clone of M3MONs/FlashscoreScraper")
SCRAPER_PYTHON = os.environ.get("FLASHSCORE_SCRAPER_PYTHON", str(Path(SCRAPER_REPO) / ".venv" / "bin" / "python"))

#: minutes before kickoff; a match becomes eligible for a window once it's
#: within `target` minutes of kickoff and hasn't been snapshotted for it yet.
WINDOWS = {"t-24h": 24 * 60, "t-1h": 60, "close": 10}
LOOKAHEAD_HOURS = 25


def already_snapshotted(session, match_id: int) -> set[str]:
    rows = session.query(Odds.snapshot).filter(Odds.match_id == match_id, Odds.snapshot.isnot(None)).distinct().all()
    return {r[0] for r in rows}


def fetch_odds(match_url: str) -> dict | None:
    """Returns the single 1x2-odds OddsResult dict ingest_odds expects -- NOT the
    {"event_url":..., "odds":[...]} wrapper `main.py odds` prints to stdout."""
    result = subprocess.run(
        [SCRAPER_PYTHON, "main.py", "odds", match_url, "--odds", "1x2-odds", "--engine", "playwright", "--timeout", "20"],
        cwd=SCRAPER_REPO,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        print(f"  scraper call failed for {match_url}: {result.stderr[-500:]}")
        return None
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        print(f"  could not parse scraper output for {match_url}")
        return None
    odds_list = payload.get("odds") or []
    return odds_list[0] if odds_list else None


def main() -> None:
    session = SessionLocal()
    now = dt.datetime.now(dt.UTC).replace(tzinfo=None)
    horizon = now + dt.timedelta(hours=LOOKAHEAD_HOURS)

    upcoming = (
        session.query(Match)
        .filter(Match.status == MatchStatus.SCHEDULED, Match.kickoff_at.isnot(None), Match.kickoff_at.between(now, horizon), Match.url.isnot(None))
        .all()
    )
    print(f"--- {len(upcoming)} scheduled matches within {LOOKAHEAD_HOURS}h ---")

    taken = 0
    for m in upcoming:
        mins_to_kickoff = (m.kickoff_at - now).total_seconds() / 60
        done = already_snapshotted(session, m.id)

        for name, target in WINDOWS.items():
            if name in done:
                continue
            if not (0 < mins_to_kickoff <= target):
                continue

            print(f"  snapshotting {name} for {m.home_team.name} vs {m.away_team.name} ({mins_to_kickoff:.0f} min to kickoff)")
            odds_result = fetch_odds(m.url)
            if odds_result is None:
                continue
            rows = ingest_odds(session, m.url, odds_result, dt.datetime.now(dt.UTC), snapshot=name)
            print(f"    ingested {len(rows)} odds rows as snapshot={name}")
            taken += 1

    print(f"--- done: {taken} snapshots taken ---")


if __name__ == "__main__":
    main()
