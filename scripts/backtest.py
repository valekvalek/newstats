"""
Backtest the xG Poisson model against every finished match currently in the
DB, using only data that existed before each match kicked off
(predict_match(..., before=match.kickoff_at)) -- no lookahead: a team's own
strength numbers never include the match being predicted or anything after
it.

Early matches for a team (the first one or two in our "last 10" sample)
will often have little or no prior history and get skipped -- that's
expected, not a bug, and is reported separately from matches the model
actually had an opinion on.

Run from the repo root: DATABASE_URL=... .venv/bin/python scripts/backtest.py
Optional: --since 2026-09-01 --until 2026-10-14 to restrict the window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("NEWSTATS_REPO", str(Path(__file__).resolve().parent.parent)))

from newstats.db.models import Match
from newstats.db.session import SessionLocal
from newstats.stats.xg_model import predict_match


def actual_outcome(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "home"
    if home_score < away_score:
        return "away"
    return "draw"


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", type=str, default=None, help="YYYY-MM-DD, exclusive")
    p.add_argument("--chunk-days", type=int, default=14, help="Size of the chronological buckets in the trend table")
    p.add_argument("--quiet", action="store_true", help="Skip the per-match lines, print only the summary")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    session = SessionLocal()

    q = session.query(Match).filter(Match.kickoff_at.isnot(None), Match.home_score.isnot(None))
    if args.since:
        q = q.filter(Match.kickoff_at >= dt.datetime.strptime(args.since, "%Y-%m-%d"))
    if args.until:
        q = q.filter(Match.kickoff_at < dt.datetime.strptime(args.until, "%Y-%m-%d"))
    matches = q.order_by(Match.kickoff_at).all()
    min_date = matches[0].kickoff_at.date() if matches else dt.date.today()

    print(f"--- backtesting {len(matches)} finished matches (pre-match data only) ---\n")

    correct = 0
    skipped = 0
    brier_sum = 0.0
    logloss_sum = 0.0
    n_scored = 0
    exact_score_hits = 0
    home_wins_scored = 0
    buckets: dict[dt.date, list[bool]] = {}

    for m in matches:
        pred = predict_match(session, m.home_team_id, m.away_team_id, before=m.kickoff_at)
        actual = actual_outcome(m.home_score, m.away_score)

        if pred is None:
            skipped += 1
            if not args.quiet:
                print(f"{m.kickoff_at.date()}  {m.home_team.name:20s} vs {m.away_team.name:20s}  SKIPPED (no pre-match xG history)")
            continue

        probs = {"home": pred.p_home_win, "draw": pred.p_draw, "away": pred.p_away_win}
        predicted_outcome = max(probs, key=probs.get)
        hit = predicted_outcome == actual
        correct += hit
        home_wins_scored += actual == "home"

        for outcome, p in probs.items():
            y = 1.0 if outcome == actual else 0.0
            brier_sum += (p - y) ** 2
            logloss_sum += -y * math.log(max(p, 1e-9))
        n_scored += 1
        exact_score_hits += pred.most_likely_score == (m.home_score, m.away_score)

        bucket_index = (m.kickoff_at.date() - min_date).days // args.chunk_days
        bucket_key = min_date + dt.timedelta(days=bucket_index * args.chunk_days)
        buckets.setdefault(bucket_key, []).append(hit)

        if not args.quiet:
            print(
                f"{m.kickoff_at.date()}  {m.home_team.name:20s} vs {m.away_team.name:20s}  "
                f"actual={m.home_score}-{m.away_score} ({actual})  "
                f"model: H={probs['home']:.0%} D={probs['draw']:.0%} A={probs['away']:.0%}  "
                f"predicted={predicted_outcome} [{'OK' if hit else 'MISS'}]"
            )

    print()
    if n_scored:
        print(f"1X2 accuracy: {correct}/{n_scored} = {correct / n_scored:.1%}")
        print(f"  (naive 'always predict home win' baseline: {home_wins_scored}/{n_scored} = {home_wins_scored / n_scored:.1%})")
        print(f"Brier score (lower=better; 0=perfect, 0.667=uniform-random baseline): {brier_sum / n_scored:.3f}")
        print(f"Log loss (lower=better; ln(3)=1.099=uniform-random baseline): {logloss_sum / n_scored:.3f}")
        print(f"Exact scoreline hits: {exact_score_hits}/{n_scored}")
        print(f"Skipped (no pre-match xG history yet): {skipped}/{len(matches)}")

        print(f"\nAccuracy over ~{args.chunk_days}-day chunks (does it improve as more history accumulates?):")
        for key in sorted(buckets):
            hits = buckets[key]
            print(f"  {key}: {sum(hits)}/{len(hits)} = {sum(hits) / len(hits):.1%}  (n={len(hits)})")
    else:
        print("Nothing scored -- no matches had enough pre-match history.")


if __name__ == "__main__":
    main()
