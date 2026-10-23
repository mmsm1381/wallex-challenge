"""Evaluates the Kronos second opinion of `kronos_second_opinion.py`.

Questions:

* Does the forecast rank the fills by their real net result (rank
  correlation, with a permutation p-value)?
* Do the fills in the lowest third of the forecast lose more?
* As a filter: a cut chosen on the first 30 days, tested on the rest.

Usage:
    python -I research/scripts/kronos_eval.py RESULT_JSON [RESULT_JSON ...]
"""
import json
import sys

import numpy as np

SIGNALS = ["f_mean", "f_up", "f_std", "f_low"]
SPLIT_DAYS = 30


def ranks(x):
    """Returns the ranks of x (ties get the mean rank)."""
    order = np.argsort(x, kind="mergesort")
    r = np.empty(len(x))
    r[order] = np.arange(len(x))
    for v in np.unique(x):
        idx = x == v
        r[idx] = r[idx].mean()
    return r


def spearman(a, b):
    return float(np.corrcoef(ranks(a), ranks(b))[0, 1])


def describe(net):
    if len(net) == 0:
        return "n   0"
    return (f"n {len(net):3d} win {100 * (net > 0).mean():3.0f}% mean {net.mean():+.2f}% "
            f"median {np.median(net):+.2f}%")


rng = np.random.default_rng(0)
for path in sys.argv[1:]:
    rows = json.load(open(path))
    net = np.array([r["net"] for r in rows])
    t = np.array([r["t"] for r in rows])
    days = (t.max() - t.min()) / 86400
    split = t.min() + SPLIT_DAYS * 86400
    print(f"== {path.rsplit('/', 1)[-1]}: {len(rows)} fills over {days:.0f} days")
    print(f"  all fills: {describe(net)}")
    for name in SIGNALS:
        x = np.array([r[name] for r in rows])
        rho = spearman(x, net)
        perm = np.array([spearman(x, rng.permutation(net)) for _ in range(500)])
        p = float((np.abs(perm) >= abs(rho)).mean())
        print(f"  {name:6s} rank corr with net {rho:+.3f} (p {p:.2f})")
        cuts = np.quantile(x, [1 / 3, 2 / 3])
        for label, mask in (("low third ", x <= cuts[0]),
                            ("mid third ", (x > cuts[0]) & (x <= cuts[1])),
                            ("high third", x > cuts[1])):
            print(f"      {label}: {describe(net[mask])}")
    # Filter test: skip fills with f_up below a cut. The cut maximizes the
    # net sum on the first 30 days.
    x = np.array([r["f_up"] for r in rows])
    train, test = t < split, t >= split
    best = max(np.unique(x), key=lambda c: net[train & (x >= c)].sum())
    test_days = (t.max() - split) / 86400
    kept = test & (x >= best)
    print(f"  filter f_up >= {best:.2f} (chosen on the first {SPLIT_DAYS} days), last "
          f"{test_days:.0f} days:")
    print(f"      no filter : {describe(net[test])} net/day {10 * net[test].sum() / test_days:+.1f}k")
    print(f"      with cut  : {describe(net[kept])} net/day {10 * net[kept].sum() / test_days:+.1f}k")
    print()
