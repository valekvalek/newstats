"""
Backtest the xG Poisson model against every finished match currently in the
DB, using only data that existed before each match kicked off
(predict_match(..., before=match.kickoff_at)) -- no lookahead: a team's own
strength numbers never include the match being predicted or anything after
it.

Only matches with status_raw == "Finished" (plain 90-minute result) are
scored for 1X2 purposes -- NOT every MatchStatus.FINISHED match. The 1X2
market settles on the 90-minute score, and:
  - "After 90 minutes" (decided by penalties with no extra time) -- the
    stored score IS the 90-minute score, so it's actually fine for 1X2;
    excluded anyway for simplicity, it's a single match on current data.
  - "After Extra Time" -- the stored score is the full 120-minute result,
    which does NOT represent the 90-minute 1X2 outcome and can't be
    converted back to one (we don't scrape a separate 90'-mark score).
    This one genuinely must be excluded, not just "could be".
See README "Известные ограничения".

Early matches for a team (the first one or two in our "last 10" sample)
will often have little or no prior history and get skipped -- that's
expected, not a bug, and is reported separately from matches the model
actually had an opinion on.

The market comparison uses whatever odds are already in the DB (via
ingest-odds-cmd) -- there is no guarantee of *when* those odds were
recorded relative to kickoff (no snapshot discipline yet, see
scripts/snapshot_odds.py), so treat "model vs market" here as a rough
orientation, not a closing-line comparison.

Run from the repo root: DATABASE_URL=... .venv/bin/python scripts/backtest.py
Optional: --since 2026-09-01 --until 2026-10-14 to restrict the window.
"""

from __future__ import annotations

import argparse
import datetime as dt
import itertools
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("NEWSTATS_REPO", str(Path(__file__).resolve().parent.parent)))

from newstats.db.models import Match, MatchStatus
from newstats.db.session import SessionLocal
from newstats.stats.metrics import (
    OUTCOME_IDX,
    actual_outcome,
    bootstrap_ci,
    brier3,
    logloss3,
)
from newstats.stats.value_bets import latest_1x2_odds
from newstats.stats.xg_model import devig_1x2, predict_match, predict_match_fitted


def market_probs(session, match_id: int) -> tuple[float, float, float] | None:
    """Average de-vigged (Home, Draw, Away) probability across whatever bookmakers we have odds for."""
    books = latest_1x2_odds(session, match_id)
    if not books:
        return None
    ps = [devig_1x2(o["Home"], o["Draw"], o["Away"]) for o in books.values()]
    return tuple(sum(p[i] for p in ps) / len(ps) for i in range(3))


def load_matches(session, since: str | None = None, until: str | None = None) -> list[Match]:
    """
    Matches with status_raw == "Finished" (plain 90-minute result, no ET/pens --
    see module docstring for why that's the right filter for 1X2 purposes, not
    MatchStatus.FINISHED) with a score and a kickoff time -- the actual
    filtering logic scripts/backtest.py runs on, pulled out so it's testable
    without going through main()/argparse. `since`/`until` are "YYYY-MM-DD"
    strings, same as the CLI flags.
    """
    q = session.query(Match).filter(
        Match.kickoff_at.isnot(None),
        Match.home_score.isnot(None),
        Match.status_raw == "Finished",
    )
    if since:
        q = q.filter(Match.kickoff_at >= dt.datetime.strptime(since, "%Y-%m-%d"))
    if until:
        q = q.filter(Match.kickoff_at < dt.datetime.strptime(until, "%Y-%m-%d"))
    return q.order_by(Match.kickoff_at).all()


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", type=str, default=None, help="YYYY-MM-DD, exclusive")
    p.add_argument("--chunk-days", type=int, default=14, help="Size of the chronological buckets in the trend table")
    p.add_argument("--quiet", action="store_true", help="Skip the per-match lines, print only the summary")
    p.add_argument(
        "--method",
        choices=["average", "fitted"],
        default="average",
        help="'average' = predict_match (shrunk team-vs-league-average); 'fitted' = predict_match_fitted (opponent-adjusted iterative fit, see its docstring for when NOT to trust it)",
    )
    p.add_argument("--xi", type=float, default=0.0, help="Recency decay for --method fitted (0 = no decay)")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    session = SessionLocal()

    matches = load_matches(session, since=args.since, until=args.until)
    q_unfiltered = session.query(Match).filter(
        Match.kickoff_at.isnot(None), Match.home_score.isnot(None), Match.status == MatchStatus.FINISHED
    )
    if args.since:
        q_unfiltered = q_unfiltered.filter(Match.kickoff_at >= dt.datetime.strptime(args.since, "%Y-%m-%d"))
    if args.until:
        q_unfiltered = q_unfiltered.filter(Match.kickoff_at < dt.datetime.strptime(args.until, "%Y-%m-%d"))
    non_90min_excluded = q_unfiltered.count() - len(matches)
    min_date = matches[0].kickoff_at.date() if matches else dt.date.today()

    print(
        f"--- backtesting {len(matches)} plain-90-minute FINISHED matches "
        f"(pre-match data only; {non_90min_excluded} ET/penalty-decided matches excluded) ---\n"
    )

    skipped = 0
    hits: list[float] = []
    model_brier: list[float] = []
    model_ll: list[float] = []
    ll_diffs: list[float] = []  # model logloss - market logloss, only for matches with both; >0 = model worse than market
    exact_score_hits = 0
    home_wins_scored = 0
    over25_hits = 0
    over25_actual = 0
    buckets: dict[dt.date, list[bool]] = {}
    calibration_points: list[tuple[float, float]] = []  # (predicted_prob, 1.0-if-that-outcome-happened)

    for m in matches:
        if args.method == "fitted":
            pred = predict_match_fitted(session, m.home_team_id, m.away_team_id, before=m.kickoff_at, xi=args.xi)
        else:
            pred = predict_match(session, m.home_team_id, m.away_team_id, before=m.kickoff_at)
        actual = actual_outcome(m.home_score, m.away_score)
        k = OUTCOME_IDX[actual]

        if pred is None:
            skipped += 1
            if not args.quiet:
                print(f"{m.kickoff_at.date()}  {m.home_team.name:20s} vs {m.away_team.name:20s}  SKIPPED (no pre-match xG history)")
            continue

        probs = (pred.p_home_win, pred.p_draw, pred.p_away_win)
        probs_named = {"home": probs[0], "draw": probs[1], "away": probs[2]}
        predicted_outcome = max(probs_named, key=probs_named.get)
        hit = predicted_outcome == actual
        hits.append(1.0 if hit else 0.0)
        home_wins_scored += actual == "home"

        model_brier.append(brier3(probs, k))
        model_ll.append(logloss3(probs, k))
        for i, p in enumerate(probs):
            calibration_points.append((p, 1.0 if i == k else 0.0))
        exact_score_hits += pred.most_likely_score == (m.home_score, m.away_score)

        actual_over25 = (m.home_score + m.away_score) > 2.5
        over25_actual += actual_over25
        over25_hits += (pred.p_over_2_5 > 0.5) == actual_over25

        mp = market_probs(session, m.id)
        if mp is not None:
            ll_diffs.append(logloss3(probs, k) - logloss3(mp, k))

        bucket_index = (m.kickoff_at.date() - min_date).days // args.chunk_days
        bucket_key = min_date + dt.timedelta(days=bucket_index * args.chunk_days)
        buckets.setdefault(bucket_key, []).append(hit)

        if not args.quiet:
            mkt_str = f"  market: H={mp[0]:.0%} D={mp[1]:.0%} A={mp[2]:.0%}" if mp else ""
            print(
                f"{m.kickoff_at.date()}  {m.home_team.name:20s} vs {m.away_team.name:20s}  "
                f"actual={m.home_score}-{m.away_score} ({actual})  "
                f"model: H={probs_named['home']:.0%} D={probs_named['draw']:.0%} A={probs_named['away']:.0%}  "
                f"predicted={predicted_outcome} [{'OK' if hit else 'MISS'}]{mkt_str}"
            )

    n_scored = len(hits)
    print()
    if n_scored:
        acc = sum(hits) / n_scored
        acc_lo, acc_hi = bootstrap_ci(hits)
        print(f"1X2 accuracy: {sum(hits):.0f}/{n_scored} = {acc:.1%}  95% CI [{acc_lo:.1%}, {acc_hi:.1%}]")
        print(f"  (naive 'always predict home win' baseline: {home_wins_scored}/{n_scored} = {home_wins_scored / n_scored:.1%})")

        brier_mean = sum(model_brier) / n_scored
        brier_lo, brier_hi = bootstrap_ci(model_brier)
        print(f"Brier score: {brier_mean:.3f}  95% CI [{brier_lo:.3f}, {brier_hi:.3f}]  (0=perfect, 0.667=uniform-random baseline)")

        ll_mean = sum(model_ll) / n_scored
        ll_lo, ll_hi = bootstrap_ci(model_ll)
        print(f"Log loss: {ll_mean:.3f}  95% CI [{ll_lo:.3f}, {ll_hi:.3f}]  (ln(3)=1.099=uniform-random baseline)")

        print(f"Exact scoreline hits: {exact_score_hits}/{n_scored}")
        print(f"Over 2.5 accuracy: {over25_hits}/{n_scored} = {over25_hits / n_scored:.1%}  (actual over rate: {over25_actual / n_scored:.1%})")
        print(f"Skipped (no pre-match xG history yet): {skipped}/{len(matches)}")

        print(f"\nMatches with market odds available: {len(ll_diffs)}/{n_scored}")
        if ll_diffs:
            diff_mean = sum(ll_diffs) / len(ll_diffs)
            diff_lo, diff_hi = bootstrap_ci(ll_diffs)
            print(f"Δ log loss (model - market): {diff_mean:+.3f}  95% CI [{diff_lo:+.3f}, {diff_hi:+.3f}]  n={len(ll_diffs)}  (negative = model beats market)")
        print("(Odds snapshot timing isn't tracked yet -- see scripts/snapshot_odds.py -- so this is an orientation, not a closing-line comparison.)")

        print(f"\nAccuracy over ~{args.chunk_days}-day chunks (does it improve as more history accumulates?):")
        for key in sorted(buckets):
            bucket_hits = buckets[key]
            print(f"  {key}: {sum(bucket_hits)}/{len(bucket_hits)} = {sum(bucket_hits) / len(bucket_hits):.1%}  (n={len(bucket_hits)})")

        print("\nCalibration (predicted prob. for an outcome vs how often it actually happened, pooled over home/draw/away):")
        n_bins = 5
        bin_edges = [i / n_bins for i in range(n_bins + 1)]
        for lo, hi in itertools.pairwise(bin_edges):
            in_bin = [(p, y) for p, y in calibration_points if lo <= p < hi or (hi == 1.0 and p == 1.0)]
            if not in_bin:
                print(f"  [{lo:.0%}, {hi:.0%}): n=0")
                continue
            mean_pred = sum(p for p, _ in in_bin) / len(in_bin)
            actual_rate = sum(y for _, y in in_bin) / len(in_bin)
            print(f"  [{lo:.0%}, {hi:.0%}): predicted avg={mean_pred:.1%}  actual rate={actual_rate:.1%}  n={len(in_bin)}")
    else:
        print("Nothing scored -- no matches had enough pre-match history.")


if __name__ == "__main__":
    main()
