"""Quality check of lagcatcher raw files: coverage, gaps, crossed books."""
import collections
import datetime
import gzip
import json
import pathlib
import sys
import zlib


def hm(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).strftime("%H:%M:%S")


def records(path):
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        try:
            for line in fh:
                if line.endswith("\n"):
                    yield json.loads(line)
        except (EOFError, zlib.error):
            return


raw = pathlib.Path(sys.argv[1])
gap_s = float(sys.argv[2]) if len(sys.argv) > 2 else 30.0

for prefix, key in (("wallex_book", "m"), ("wallex_trade", "m"), ("global", None)):
    count = collections.Counter()
    last = {}
    max_gap = collections.defaultdict(float)
    big_gaps = collections.defaultdict(list)
    first_ts = last_ts = None
    files = sorted(raw.glob(f"{prefix}-*.jsonl.gz"))
    for path in files:
        for rec in records(path):
            ts = rec["ts"]
            first_ts = ts if first_ts is None else first_ts
            last_ts = ts
            k = rec[key] if key else f'{rec["v"]}:{rec["c"]}'
            count[k] += 1
            if k in last:
                gap = ts - last[k]
                max_gap[k] = max(max_gap[k], gap)
                if gap > gap_s:
                    big_gaps[k].append((hm(last[k]), hm(ts), round(gap)))
            last[k] = ts
    print(f"\n== {prefix}: {len(files)} files, {sum(count.values())} records, "
          f"{hm(first_ts)} to {hm(last_ts)} UTC")
    for k in sorted(count):
        gaps = big_gaps[k]
        extra = f"  gaps>{gap_s:g}s: {gaps[:4]}{' ...' if len(gaps) > 4 else ''}" if gaps else ""
        print(f"  {k:16s} n={count[k]:7d} max_gap={max_gap[k]:7.1f}s{extra}")

# Crossed or locked Wallex books (best bid >= best ask), per market.
best = collections.defaultdict(dict)
crossed = collections.Counter()
seen = collections.Counter()
for path in sorted(raw.glob("wallex_book-*.jsonl.gz")):
    for rec in records(path):
        m = rec["m"]
        best[m][rec["s"]] = rec["lv"][0][0] if rec["lv"] else None
        b, a = best[m].get("bid"), best[m].get("ask")
        if b and a:
            seen[m] += 1
            if b >= a:
                crossed[m] += 1
print("\n== crossed or locked Wallex books (share of updates)")
for m in sorted(seen):
    if crossed[m]:
        print(f"  {m:10s} {crossed[m]} of {seen[m]} ({100 * crossed[m] / seen[m]:.2f}%)")
print("  (only markets with at least one are listed)")
