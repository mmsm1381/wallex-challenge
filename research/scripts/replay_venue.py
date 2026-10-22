"""Replay with the fair value from one global venue only.

Usage (from the repo root, with PYTHONPATH=.): python research/scripts/replay_venue.py RAW_DIR OUT_DIR CONFIG VENUE
The same variant grid as `lagcatcher replay`. Quotes of other venues are
dropped, and one fresh venue is enough for a fair value.
"""
import logging
import pathlib
import sys

from lagcatcher import config, engine, replay, report

raw_dir, out_dir, cfg_path, venue = sys.argv[1:5]
logging.getLogger("lagcatcher").setLevel(logging.WARNING)
cfg = config.load_config(cfg_path)
cfg["run"]["out_dir"] = out_dir
cfg["venues"]["enabled"] = [venue]
cfg["venues"]["min_venues"] = 1
cfg["episodes"]["thresholds_pct"] = [0.15, 0.25, 0.35, 0.5]
pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
for name in ("events.jsonl", "episodes.jsonl"):
    (pathlib.Path(out_dir) / name).unlink(missing_ok=True)
variants = replay.build_variants(cfg, buffers_pct=[0.0, 0.05, 0.12, 0.22, 0.37],
                                 latencies_s=[0.3, 1.0],
                                 resting_buffers_pct=[0.05, 0.15, 0.3])
live = engine.Engine(cfg, strategies=variants)
live.recorder.raw_enabled = False
records = (r for r in replay.load_records(raw_dir)
           if r[1] != "quote" or r[2]["v"] == venue)
replay.run(live, records)
live.recorder.flush()
live.recorder.close()
report.main(out_dir)
