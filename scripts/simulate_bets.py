"""
Flat-stake bet simulation: for every match with status_raw == "Finished"
(plain 90-minute result -- see scripts/backtest.py's module docstring for
why that's the right filter, not just MatchStatus.FINISHED) that has BOTH
a --snapshot quote and a snapshot="close" quote from a specific bookmaker,
bet on the single highest-EV outcome at the --snapshot price if its EV
clears a fixed threshold -- set BEFORE looking at results, never tuned
against them.

Betting at snapshot="close" itself would be circular: devig_1x2(close
odds) IS (up to the bookmaker's margin) the market's own close-time
probability, so an "edge" against it is just -margin, not signal. Betting
requires a price from *before* the close (default --snapshot t-1h) and
*comparing* it to the close for CLV -- hence requiring both.

The model is evaluated with before=<the snapshot's own recorded_at>, not
the match's kickoff_at: a bet "placed" at t-24h must only see data that
existed at t-24h, not everything that accumulated between then and kickoff.

At most one bet per match. profit = odds-1 on a win, -STAKE on a loss.
Reports n, ROI with a bootstrap 95% CI, and mean CLV (always computable
here, since a close snapshot is required to bet at all).

On n in the hundreds, ROI is close to pure noise; the CI is the point.
Rule of thumb from the brief: treat this as "there's a signal" only once
mean CLV is stably positive on >=300 bets, not from ROI alone.

Run from the repo root: DATABASE_URL=... .venv/bin/python scripts/simulate_bets.py
Optional: --ev-min 0.05 --stake 1.0 --book bet365 --snapshot t-1h --since 2026-09-01
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("NEWSTATS_REPO", str(Path(__file__).resolve().parent.parent)))

from backtest import load_matches

from newstats.db.session import SessionLocal
from newstats.stats.metrics import actual_outcome, bootstrap_ci
from newstats.stats.value_bets import (
    clv,
    devig_1x2,
    latest_1x2_odds,
    snapshot_recorded_at,
)
from newstats.stats.xg_model import predict_match

SELECTIONS = ("Home", "Draw", "Away")
_OUTCOME_TO_SELECTION = {"home": "Home", "draw": "Draw", "away": "Away"}


def decide_bet(probs: dict[str, float], odds: dict[str, float], ev_min: float) -> tuple[str, float] | None:
    """Highest-EV selection if it clears ev_min, else None. At most one bet per match."""
    ev = {s: probs[s] * odds[s] - 1 for s in SELECTIONS}
    selection, best_ev = max(ev.items(), key=lambda kv: kv[1])
    return (selection, best_ev) if best_ev >= ev_min else None


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ev-min", type=float, default=0.05, help="Minimum edge (model_prob * odds - 1) to place a bet")
    p.add_argument("--stake", type=float, default=1.0)
    p.add_argument("--book", type=str, default="bet365", help="Only bet against this bookmaker's odds")
    p.add_argument("--snapshot", type=str, default="t-1h", choices=["t-24h", "t-1h"], help="Which pre-close snapshot to bet at")
    p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", type=str, default=None, help="YYYY-MM-DD, exclusive")
    p.add_argument("--csv", type=str, default="simulated_bets.csv", help="Audit trail output path")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    session = SessionLocal()

    matches = load_matches(session, since=args.since, until=args.until)
    print(
        f"--- simulating flat {args.stake:.2f}-unit bets at snapshot={args.snapshot!r}, "
        f"EV_MIN={args.ev_min:.0%}, book={args.book!r}, over {len(matches)} plain-90-minute FINISHED matches ---\n"
    )

    bets: list[dict] = []
    no_odds = 0
    no_pred = 0

    for m in matches:
        books = latest_1x2_odds(session, m.id, snapshot=args.snapshot)
        close_books = latest_1x2_odds(session, m.id, snapshot="close")
        if args.book not in books or args.book not in close_books:
            no_odds += 1
            continue
        odds = books[args.book]

        bet_time = snapshot_recorded_at(session, m.id, args.book, args.snapshot)
        pred = predict_match(session, m.home_team_id, m.away_team_id, before=bet_time)
        if pred is None:
            no_pred += 1
            continue

        probs = {"Home": pred.p_home_win, "Draw": pred.p_draw, "Away": pred.p_away_win}
        decision = decide_bet(probs, odds, args.ev_min)
        if decision is None:
            continue
        selection, best_ev = decision

        actual = _OUTCOME_TO_SELECTION[actual_outcome(m.home_score, m.away_score)]
        won = selection == actual
        profit = (odds[selection] - 1) * args.stake if won else -args.stake

        p_close = devig_1x2(close_books[args.book]["Home"], close_books[args.book]["Draw"], close_books[args.book]["Away"])
        p_close_selection = dict(zip(SELECTIONS, p_close))[selection]
        bet_clv = clv(odds[selection], p_close_selection)

        bets.append(
            {
                "match_id": m.id,
                "bet_time": bet_time,
                "kickoff_at": m.kickoff_at,
                "home_team": m.home_team.name,
                "away_team": m.away_team.name,
                "selection": selection,
                "odds": odds[selection],
                "model_prob": probs[selection],
                "ev": best_ev,
                "won": won,
                "profit": profit,
                "clv": bet_clv,
            }
        )

    if bets:
        with open(args.csv, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(bets[0].keys()))
            writer.writeheader()
            writer.writerows(bets)
        print(f"Audit trail written to {args.csv}\n")

    n = len(bets)
    print(
        f"Bets placed: {n}/{len(matches)}  "
        f"(skipped: {no_pred} no pre-match xG history at bet time, {no_odds} missing {args.snapshot!r} or 'close' {args.book!r} odds)"
    )
    if n == 0:
        print("\nNothing to report -- 0 bets cleared the EV threshold (or the required snapshots don't exist yet).")
        if no_odds == len(matches) - no_pred:
            print(f"No match had both a {args.snapshot!r} and a 'close' {args.book!r} quote -- run scripts/snapshot_odds.py for a while first.")
        return

    profits = [b["profit"] for b in bets]
    total_staked = n * args.stake
    roi = sum(profits) / total_staked
    roi_lo, roi_hi = bootstrap_ci([p / args.stake for p in profits])
    print(f"ROI: {roi:+.1%}  95% CI [{roi_lo:+.1%}, {roi_hi:+.1%}]  (n={n}, staked={total_staked:.0f})")
    print(f"Win rate: {sum(b['won'] for b in bets)}/{n} = {sum(b['won'] for b in bets) / n:.1%}")

    clvs = [b["clv"] for b in bets]
    clv_mean = sum(clvs) / len(clvs)
    clv_lo, clv_hi = bootstrap_ci(clvs)
    print(f"Mean CLV: {clv_mean:+.1%}  95% CI [{clv_lo:+.1%}, {clv_hi:+.1%}]  (n={len(clvs)})")

    print('\nTreat ROI as noise until n is in the hundreds; the brief\'s bar for "there\'s a signal" is CLV stably positive on >=300 bets, not ROI alone.')


if __name__ == "__main__":
    main()
