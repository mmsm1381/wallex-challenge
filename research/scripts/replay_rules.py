"""Replay of all raw data, with the new rules on or off.

Usage (from the repo root):
    PYTHONPATH=. .venv/bin/python research/scripts/replay_rules.py RAW_DIR OUT_DIR CONFIG on|off [STOP_CONFIRM_S]
"off" sets the old rules: stop on the first low bid, no cooldown, and
premium samples at every spread.
"""
import logging
import pathlib
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
variants = replay.build_variants(cfg, buffers_pct=[0.0, 0.05, 0.12, 0.22, 0.37],
                                 latencies_s=[0.3, 1.0],
                                 resting_buffers_pct=[0.05, 0.15, 0.3])
live = engine.Engine(cfg, strategies=variants)
live.recorder.raw_enabled = False
replay.run(live, replay.load_records(raw_dir))
live.recorder.flush()
live.recorder.close()
report.main(out_dir)
