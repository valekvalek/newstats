import math

from newstats.stats.metrics import bootstrap_ci, brier3, logloss3


def test_brier3_perfect_prediction_is_zero():
    assert brier3((1, 0, 0), 0) == 0


def test_brier3_penalizes_wrong_confident_prediction():
    assert brier3((1, 0, 0), 2) == 2  # (1-0)^2 + (0-0)^2 + (0-1)^2 = 1 + 1 = 2


def test_logloss3_uniform_prediction_is_ln3():
    assert math.isclose(logloss3((1 / 3, 1 / 3, 1 / 3), 0), math.log(3), rel_tol=1e-9)


def test_logloss3_perfect_prediction_is_near_zero():
    assert logloss3((1.0, 0.0, 0.0), 0) < 1e-6


def test_bootstrap_ci_brackets_the_true_mean_for_constant_data():
    lo, hi = bootstrap_ci([5.0] * 50)
    assert lo == hi == 5.0


def test_bootstrap_ci_is_narrower_for_larger_samples():
    rng_data_small = [0.0, 1.0] * 5  # n=10, high variance in the mean estimate
    rng_data_large = [0.0, 1.0] * 500  # n=1000
    lo_s, hi_s = bootstrap_ci(rng_data_small, seed=1)
    lo_l, hi_l = bootstrap_ci(rng_data_large, seed=1)
    assert (hi_l - lo_l) < (hi_s - lo_s)
