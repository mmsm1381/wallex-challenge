"""Replay of all raw data with resting bids only, at the depths in BUFS.

A buffer B gives a bid at fair value x (1 - 2 x maker fee - B). The real
depth below fair value is B + 0.5% on toman markets and B + 0.2% on USDT
markets.
Usage (from the repo root):
    BUFS=0.3,0.6,0.8,1.0 PYTHONPATH=. .venv/bin/python \
        research/scripts/replay_deep_bids.py RAW_DIR OUT_DIR CONFIG on
"""
import logging
import pathlib
import os
import sys

from lagcatcher import config, engine, replay, report

raw_dir, out_dir, cfg_path, mode = sys.argv[1:5]
logging.getLogger("lagcatcher").setLevel(logging.WARNING)
cfg = config.load_config(cfg_path)
cfg["run"]["out_dir"] = out_dir
cfg["episodes"]["thresholds_pct"] = [0.15, 0.25, 0.35, 0.5]
if len(sys.argv) > 5:
    cfg["exit"]["stop_confirm_s"] = float(sys.argv[5])
if mode == "off":
    cfg["exit"].update(stop_confirm_s=0.0, cooldown_s=0.0)
    cfg["premium"]["max_spread_pct"] = 1e9
    cfg["paper"]["max_entry_spread_pct"] = 1e9
pathlib.Path(out_dir).mkdir(parents=True, exist_ok=True)
for name in ("events.jsonl", "episodes.jsonl"):
    (pathlib.Path(out_dir) / name).unlink(missing_ok=True)
variants = replay.build_variants(cfg, buffers_pct=[],
                                 latencies_s=[0.3, 1.0],
                                 resting_buffers_pct=[float(x) for x in os.environ["BUFS"].split(",")])
live = engine.Engine(cfg, strategies=variants)
live.recorder.raw_enabled = False
replay.run(live, replay.load_records(raw_dir))
live.recorder.flush()
live.recorder.close()
report.main(out_dir)
