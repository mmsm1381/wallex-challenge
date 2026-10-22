"""Time-weighted Wallex spread per market from lagcatcher raw book files."""
import collections
import gzip
import json
import pathlib
import sys
import zlib


def records(raw):
    for path in sorted(pathlib.Path(raw).glob("wallex_book-*.jsonl.gz")):
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            try:
                for line in fh:
                    if line.endswith("\n"):
                        yield json.loads(line)
            except (EOFError, zlib.error):
                pass


best = collections.defaultdict(dict)
last_ts = {}
hist = collections.defaultdict(list)  # market -> [(spread_pct, seconds)]
for rec in records(sys.argv[1]):
    m, ts = rec["m"], rec["ts"]
    b, a = best[m].get("bid"), best[m].get("ask")
    if b and a and m in last_ts:
        hist[m].append((100 * (a / b - 1), min(ts - last_ts[m], 60.0)))
    best[m][rec["s"]] = rec["lv"][0][0] if rec["lv"] else None
    last_ts[m] = ts


def quantile(pairs, q):
    total = sum(w for _, w in pairs)
    acc = 0.0
    for value, weight in sorted(pairs):
        acc += weight
        if acc >= q * total:
            return value
    return float("nan")


print(f"{'market':10s} {'p10':>6s} {'median':>7s} {'p90':>6s} {'max':>6s}  share>0.3%")
for m in sorted(hist):
    pairs = hist[m]
    total = sum(w for _, w in pairs) or 1.0
    wide = sum(w for s, w in pairs if s > 0.3) / total
    print(f"{m:10s} {quantile(pairs, .1):6.3f} {quantile(pairs, .5):7.3f} "
          f"{quantile(pairs, .9):6.3f} {max(s for s, _ in pairs):6.2f}  {100 * wide:5.1f}%")
