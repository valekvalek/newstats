"""
Backtest the xG Poisson model against the 9 Champions League Round-2
matches played on 2026-10-13, using only data that existed before each
match kicked off (predict_match(..., before=match.kickoff_at)) so there's
no lookahead -- a team's own strength numbers never include the match
being predicted, or anything after it.

Reports, per match: predicted 1X2 probabilities + most-likely score vs what
actually happened, plus aggregate accuracy, Brier score and log loss across
the 9 matches.

Run from the repo root: DATABASE_URL=... .venv/bin/python scripts/backtest_oct13.py
"""

from __future__ import annotations

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


def main() -> None:
    session = SessionLocal()
    matches = (
        session.query(Match)
        .filter(
            Match.kickoff_at >= dt.datetime(2026, 10, 13),
            Match.kickoff_at < dt.datetime(2026, 10, 14),
            Match.home_score.isnot(None),
        )
        .order_by(Match.kickoff_at)
        .all()
    )

    print(f"--- backtesting {len(matches)} matches from 2026-10-13 (pre-match data only) ---\n")

    correct = 0
    skipped = 0
    brier_sum = 0.0
    logloss_sum = 0.0
    n_scored = 0
    exact_score_hits = 0

    for m in matches:
        pred = predict_match(session, m.home_team_id, m.away_team_id, before=m.kickoff_at)
        actual = actual_outcome(m.home_score, m.away_score)

        if pred is None:
            print(f"{m.home_team.name:20s} vs {m.away_team.name:20s}  SKIPPED (no pre-match xG history for one side)")
            skipped += 1
            continue

        probs = {"home": pred.p_home_win, "draw": pred.p_draw, "away": pred.p_away_win}
        predicted_outcome = max(probs, key=probs.get)
        hit = predicted_outcome == actual
        correct += hit

        # Brier score (multi-class, sum of squared errors over the 3 outcomes) and log loss
        for outcome, p in probs.items():
            y = 1.0 if outcome == actual else 0.0
            brier_sum += (p - y) ** 2
            logloss_sum += -y * math.log(max(p, 1e-9))
        n_scored += 1

        exact = pred.most_likely_score == (m.home_score, m.away_score)
        exact_score_hits += exact

        print(
            f"{m.home_team.name:20s} vs {m.away_team.name:20s}  "
            f"actual={m.home_score}-{m.away_score} ({actual})  "
            f"model: H={probs['home']:.0%} D={probs['draw']:.0%} A={probs['away']:.0%}  "
            f"predicted={predicted_outcome} [{'OK' if hit else 'MISS'}]  "
            f"most-likely-score={pred.most_likely_score[0]}-{pred.most_likely_score[1]}"
            f"{' (exact!)' if exact else ''}"
        )

    home_wins = sum(1 for m in matches if m.home_score is not None and actual_outcome(m.home_score, m.away_score) == "home")

    print()
    if n_scored:
        print(f"1X2 accuracy: {correct}/{n_scored} = {correct / n_scored:.1%}")
        print(f"  (naive 'always predict home win' baseline on these matches: {home_wins}/{len(matches)} = {home_wins / len(matches):.1%})")
        print(f"Brier score (3-outcome, lower=better; 0=perfect, 0.667=uniform-random baseline): {brier_sum / n_scored:.3f}")
        print(f"Log loss (lower=better; ln(3)=1.099=uniform-random baseline): {logloss_sum / n_scored:.3f}")
        print(f"Exact scoreline hits: {exact_score_hits}/{n_scored}")
        print(f"\nn={n_scored} -- far too small to draw real conclusions from; this is a sanity check, not a validated backtest.")
    if skipped:
        print(f"Skipped: {skipped} (no pre-match xG history)")


if __name__ == "__main__":
    main()
