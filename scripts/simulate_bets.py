"""
Flat-stake bet simulation: for every FINISHED (non-penalty-decided) match
with a pre-match prediction and odds from a specific bookmaker, bet on the
single highest-EV outcome if its EV clears a fixed threshold -- set BEFORE
looking at results, never tuned against them.

At most one bet per match. profit = odds-1 on a win, -STAKE on a loss.
Reports n, ROI with a bootstrap 95% CI, and mean CLV wherever a closing
snapshot (snapshot="close") is available for that match (see
scripts/snapshot_odds.py) -- without it CLV can't be computed at all, just
left blank in the CSV.

On n in the hundreds, ROI is close to pure noise; the CI is the point.
Rule of thumb from the brief: treat this as "there's a signal" only once
mean CLV is stably positive on >=300 bets, not from ROI alone.

Run from the repo root: DATABASE_URL=... .venv/bin/python scripts/simulate_bets.py
Optional: --ev-min 0.05 --stake 1.0 --book bet365 --since 2026-09-01
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import os
import sys
from pathlib import Path

sys.path.insert(0, os.environ.get("NEWSTATS_REPO", str(Path(__file__).resolve().parent.parent)))

from newstats.db.models import Match, MatchStatus
from newstats.db.session import SessionLocal
from newstats.importer.flashscore import is_penalty_decided
from newstats.stats.metrics import bootstrap_ci
from newstats.stats.value_bets import clv, latest_1x2_odds
from newstats.stats.xg_model import devig_1x2, predict_match

SELECTIONS = ("Home", "Draw", "Away")


def actual_outcome(home_score: int, away_score: int) -> str:
    if home_score > away_score:
        return "Home"
    if home_score < away_score:
        return "Away"
    return "Draw"


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
    p.add_argument("--since", type=str, default=None, help="YYYY-MM-DD, inclusive")
    p.add_argument("--until", type=str, default=None, help="YYYY-MM-DD, exclusive")
    p.add_argument("--csv", type=str, default="simulated_bets.csv", help="Audit trail output path")
    return p.parse_args()


def main() -> None:
    args = parse_args()
    session = SessionLocal()

    q = session.query(Match).filter(
        Match.kickoff_at.isnot(None),
        Match.home_score.isnot(None),
        Match.status == MatchStatus.FINISHED,
    )
    if args.since:
        q = q.filter(Match.kickoff_at >= dt.datetime.strptime(args.since, "%Y-%m-%d"))
    if args.until:
        q = q.filter(Match.kickoff_at < dt.datetime.strptime(args.until, "%Y-%m-%d"))
    matches = [m for m in q.order_by(Match.kickoff_at).all() if not is_penalty_decided(m.status_raw)]

    print(f"--- simulating flat {args.stake:.2f}-unit bets, EV_MIN={args.ev_min:.0%}, book={args.book!r}, over {len(matches)} FINISHED matches ---\n")

    bets: list[dict] = []
    no_odds = 0
    no_pred = 0

    for m in matches:
        pred = predict_match(session, m.home_team_id, m.away_team_id, before=m.kickoff_at)
        if pred is None:
            no_pred += 1
            continue

        books = latest_1x2_odds(session, m.id)
        if args.book not in books:
            no_odds += 1
            continue
        odds = books[args.book]

        probs = {"Home": pred.p_home_win, "Draw": pred.p_draw, "Away": pred.p_away_win}
        decision = decide_bet(probs, odds, args.ev_min)
        if decision is None:
            continue
        selection, best_ev = decision

        actual = actual_outcome(m.home_score, m.away_score)
        won = selection == actual
        profit = (odds[selection] - 1) * args.stake if won else -args.stake

        close_books = latest_1x2_odds(session, m.id, snapshot="close")
        bet_clv = None
        if args.book in close_books:
            p_close = devig_1x2(close_books[args.book]["Home"], close_books[args.book]["Draw"], close_books[args.book]["Away"])
            p_close_selection = dict(zip(SELECTIONS, p_close))[selection]
            bet_clv = clv(odds[selection], p_close_selection)

        bets.append(
            {
                "match_id": m.id,
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
    print(f"Bets placed: {n}/{len(matches)}  (skipped: {no_pred} no pre-match xG history, {no_odds} no {args.book!r} odds)")
    if n == 0:
        print("\nNothing to report -- 0 bets cleared the EV threshold (or no odds were available at all).")
        if no_odds == len(matches) - no_pred:
            print(f"All eligible matches had zero {args.book!r} odds in the DB -- this project hasn't historically snapshotted odds (see scripts/snapshot_odds.py).")
        return

    profits = [b["profit"] for b in bets]
    total_staked = n * args.stake
    roi = sum(profits) / total_staked
    roi_lo, roi_hi = bootstrap_ci([p / args.stake for p in profits])
    print(f"ROI: {roi:+.1%}  95% CI [{roi_lo:+.1%}, {roi_hi:+.1%}]  (n={n}, staked={total_staked:.0f})")
    print(f"Win rate: {sum(b['won'] for b in bets)}/{n} = {sum(b['won'] for b in bets) / n:.1%}")

    clvs = [b["clv"] for b in bets if b["clv"] is not None]
    if clvs:
        clv_mean = sum(clvs) / len(clvs)
        clv_lo, clv_hi = bootstrap_ci(clvs)
        print(f"Mean CLV: {clv_mean:+.1%}  95% CI [{clv_lo:+.1%}, {clv_hi:+.1%}]  (n={len(clvs)} bets with a closing snapshot)")
    else:
        print("Mean CLV: n/a -- no bets had a snapshot='close' quote recorded (see scripts/snapshot_odds.py).")

    print("\nTreat ROI as noise until n is in the hundreds; the brief's bar for \"there's a signal\" is CLV stably positive on >=300 bets, not ROI alone.")


if __name__ == "__main__":
    main()
