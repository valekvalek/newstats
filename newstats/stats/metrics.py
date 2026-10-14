"""Scoring metrics for the backtest: Brier score, log loss, bootstrap confidence intervals."""

from __future__ import annotations

import math
import random
from collections.abc import Sequence

OUTCOME_IDX = {"home": 0, "draw": 1, "away": 2}


def brier3(probs: Sequence[float], k: int) -> float:
    """3-outcome Brier score for one prediction: sum of squared errors vs the one-hot actual outcome `k`."""
    return sum((p - (1.0 if i == k else 0.0)) ** 2 for i, p in enumerate(probs))


def logloss3(probs: Sequence[float], k: int) -> float:
    """Log loss for one prediction against the actual outcome index `k`."""
    return -math.log(max(probs[k], 1e-9))


def bootstrap_ci(xs: Sequence[float], n_boot: int = 10_000, alpha: float = 0.05, seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap confidence interval for the mean of `xs`."""
    rng = random.Random(seed)
    m = len(xs)
    means = sorted(sum(rng.choices(xs, k=m)) / m for _ in range(n_boot))
    return means[int(alpha / 2 * n_boot)], means[int((1 - alpha / 2) * n_boot) - 1]
